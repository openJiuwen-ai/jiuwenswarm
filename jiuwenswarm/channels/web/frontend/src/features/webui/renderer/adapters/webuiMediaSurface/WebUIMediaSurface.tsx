/**
 * 媒体容器：按 kind 显示图片、视频或沙箱页面，失败、超时或减少动态时回退到封面。
 * 静音、就绪超时与角色来自宿主策略；素材地址是平台路由，直接使用以保留分段读取和页面内相对引用。
 */

import {
  useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type CSSProperties, type RefObject,
} from 'react';
import { useTranslation } from 'react-i18next';
import type { WebUIAdapterProps } from '../../types.ts';
import { useMediaSurfacePolicy, type MediaSurfaceEvent } from './hostPolicy.ts';
import {
  createSurfaceToken,
  readSurfaceMessage,
  surfaceHostMessage,
  withSurfaceToken,
  type SurfaceContext,
  type SurfaceHostEvent,
} from './surfaceProtocol.ts';
import './MediaSurface.css';

interface MediaSurfaceProps {
  kind: 'image' | 'video' | 'html';
  src: string;
  title: string;
  poster?: string;
  fit: 'cover' | 'contain';
  position: 'center' | 'top' | 'bottom' | 'left' | 'right';
  loop: boolean;
  muted: boolean;
  dim: number;
  blur: number;
  opacity: number;
}

const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)';

function subscribeReducedMotion(listener: () => void): () => void {
  const query = window.matchMedia?.(REDUCED_MOTION_QUERY);
  query?.addEventListener?.('change', listener);
  return () => query?.removeEventListener?.('change', listener);
}
const getReducedMotion = (): boolean => window.matchMedia?.(REDUCED_MOTION_QUERY).matches ?? false;
function subscribeVisibility(listener: () => void): () => void {
  document.addEventListener('visibilitychange', listener);
  return () => document.removeEventListener('visibilitychange', listener);
}
const getVisible = (): boolean => document.visibilityState !== 'hidden';
const serverFalse = (): boolean => false;
const serverTrue = (): boolean => true;

type Emit = (event: MediaSurfaceEvent) => void;

function Poster({ src, style }: { src?: string; style: CSSProperties }) {
  const [failed, setFailed] = useState(false);
  if (!src || failed) return null;
  return <img className="webui-media-surface__media" data-testid="webui-media-surface-poster" src={src} alt=""
    draggable={false} style={style} onError={() => setFailed(true)} />;
}

function SurfaceImage({ src, poster, style, emit }: { src: string; poster?: string; style: CSSProperties; emit: Emit }) {
  const [failed, setFailed] = useState(false);
  if (failed) return <Poster src={poster} style={style} />;
  return <img className="webui-media-surface__media" data-testid="webui-media-surface-image" src={src} alt=""
    draggable={false} style={style} onLoad={() => emit('ready')} onError={() => { emit('error'); setFailed(true); }} />;
}

interface SurfaceVideoProps {
  src: string;
  poster?: string;
  style: CSSProperties;
  loop: boolean;
  muted: boolean;
  autoPlay: boolean;
  visible: boolean;
  emit: Emit;
}

function SurfaceVideo({ src, poster, style, loop, muted, autoPlay, visible, emit }: SurfaceVideoProps) {
  const video = useRef<HTMLVideoElement>(null);
  const [failed, setFailed] = useState(false);
  const started = useRef(false);

  useEffect(() => {
    // React does not reflect `muted` reliably; autoplay policies read the property.
    if (video.current) video.current.muted = muted;
  }, [muted, failed]);

  useEffect(() => {
    const element = video.current;
    if (!element || !autoPlay) return;
    if (!visible) {
      element.pause();
      return;
    }
    const retryMuted = () => {
      // Autoplay with sound may be refused; playing silently still shows the media.
      if (element.muted) return;
      element.muted = true;
      void element.play()?.catch(() => undefined);
    };
    try {
      void element.play()?.catch(retryMuted);
    } catch {
      // An unsupported play() leaves the poster frame in place.
    }
  }, [autoPlay, visible, failed]);

  if (failed) return <Poster src={poster} style={style} />;
  return <video ref={video} className="webui-media-surface__media" data-testid="webui-media-surface-video"
    src={src} poster={poster} style={style} loop={loop} muted={muted} autoPlay={autoPlay && visible} playsInline
    preload="auto" controls={false} disablePictureInPicture disableRemotePlayback tabIndex={-1}
    onPlaying={() => { if (!started.current) { started.current = true; emit('ready'); } }}
    onTimeUpdate={() => emit('heartbeat')} onEnded={() => emit('complete')}
    onError={() => { emit('error'); setFailed(true); }} />;
}

interface SurfaceFrameProps {
  src: string;
  title: string;
  poster?: string;
  posterStyle: CSSProperties;
  context: SurfaceContext;
  readyTimeoutMs: number;
  /** Delegate autoplay with sound; withheld for silent surfaces so the page cannot start audio. */
  allowAudio: boolean;
  emit: Emit;
}

function SurfaceFrame({ src, title, poster, posterStyle, context, readyTimeoutMs, allowAudio, emit }: SurfaceFrameProps) {
  const frame = useRef<HTMLIFrameElement>(null);
  const token = useMemo(() => createSurfaceToken(), [src]);
  const [phase, setPhase] = useState<'loading' | 'ready' | 'failed'>('loading');
  const latest = useRef(context);
  latest.current = context;
  const contextChanged = useRef(false);

  const post = (event: SurfaceHostEvent) => {
    frame.current?.contentWindow?.postMessage(surfaceHostMessage(event, latest.current), '*');
  };

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const frameWindow = frame.current?.contentWindow;
      if (!frameWindow || event.source !== frameWindow) return;
      const surfaceEvent = readSurfaceMessage(event.data, token);
      if (surfaceEvent) emit(surfaceEvent);
      if (surfaceEvent === 'ready') {
        setPhase((current) => (current === 'failed' ? current : 'ready'));
        post('start');
      } else if (surfaceEvent === 'error') {
        setPhase('failed');
      }
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [token]);

  useEffect(() => {
    if (phase !== 'loading') return undefined;
    const timer = window.setTimeout(() => setPhase('failed'), readyTimeoutMs);
    return () => window.clearTimeout(timer);
  }, [phase, readyTimeoutMs]);

  useEffect(() => {
    // The first context goes out with start; later changes are sent on their own.
    if (!contextChanged.current) {
      contextChanged.current = true;
      return;
    }
    post('context');
  }, [context.visible, context.reducedMotion, context.locale]);

  if (phase === 'failed') return <Poster src={poster} style={posterStyle} />;
  // Transparent rather than hidden before ready, so readiness does not depend on whether an engine
  // throttles hidden cross-origin frames (Chromium 157 does not; see Gateway F_10).
  return (
    <>
      <iframe ref={frame} className="webui-media-surface__media" data-testid="webui-media-surface-frame"
        data-variant={phase} src={withSurfaceToken(src, token)} title={title} sandbox="allow-scripts"
        allow={allowAudio ? 'autoplay' : ''} referrerPolicy="no-referrer" tabIndex={-1}
        style={{ opacity: phase === 'ready' ? 1 : 0 }} onLoad={() => post('start')} />
      {phase === 'loading' ? <Poster src={poster} style={posterStyle} /> : null}
    </>
  );
}

/**
 * Scale that pushes every edge of blurred media at least twice the blur radius past the container,
 * so the blur never shows a transparent rim. Scaling keeps the media's layout size: a page behind it
 * is not resized (and does not redraw itself) while the blur changes.
 */
function blurOverscan(blur: number, size: { width: number; height: number } | null): number {
  if (!(blur > 0)) return 1;
  const shortest = Math.max(1, Math.min(size?.width || window.innerWidth || 1, size?.height || window.innerHeight || 1));
  return 1 + (4 * blur) / shortest;
}

function useElementSize(element: RefObject<HTMLElement | null>): { width: number; height: number } | null {
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  useLayoutEffect(() => {
    const target = element.current;
    if (!target || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver((entries) => {
      const box = entries[entries.length - 1]?.contentRect;
      if (box) setSize((current) => (current?.width === box.width && current?.height === box.height
        ? current : { width: box.width, height: box.height }));
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, [element]);
  return size;
}

export function WebUIMediaSurface({ node }: WebUIAdapterProps) {
  const p = node.props as unknown as MediaSurfaceProps;
  const policy = useMediaSurfacePolicy();
  const { i18n } = useTranslation();
  const reducedMotion = useSyncExternalStore(subscribeReducedMotion, getReducedMotion, serverFalse);
  const visible = useSyncExternalStore(subscribeVisibility, getVisible, serverTrue);
  const { onPresence } = policy;
  // Hosts read the latest callback without re-creating the media elements.
  const eventCallback = useRef(policy.onSurfaceEvent);
  eventCallback.current = policy.onSurfaceEvent;
  const emit = useMemo<Emit>(() => (event) => eventCallback.current?.(event), []);
  const surface = useRef<HTMLDivElement>(null);
  const size = useElementSize(surface);

  useEffect(() => {
    onPresence?.(true);
    return () => onPresence?.(false);
  }, [onPresence]);

  const mediaStyle: CSSProperties = { objectFit: p.fit, objectPosition: p.position };
  const layerStyle: CSSProperties | undefined = p.blur > 0
    ? { filter: `blur(${p.blur}px)`, transform: `scale(${blurOverscan(p.blur, size).toFixed(4)})` } : undefined;
  const posterOnly = policy.honorReducedMotion && p.kind !== 'image' && reducedMotion && !!p.poster;

  let content;
  if (p.kind === 'image') {
    content = <SurfaceImage key={p.src} src={p.src} poster={p.poster} style={mediaStyle} emit={emit} />;
  } else if (posterOnly) {
    content = <Poster key={p.poster} src={p.poster} style={mediaStyle} />;
  } else if (p.kind === 'video') {
    content = <SurfaceVideo key={p.src} src={p.src} poster={p.poster} style={mediaStyle} loop={policy.allowLoop && p.loop}
      muted={policy.forceMuted || p.muted} autoPlay={!(policy.honorReducedMotion && reducedMotion)} visible={visible}
      emit={emit} />;
  } else {
    content = <SurfaceFrame key={p.src} src={p.src} title={p.title} poster={p.poster} posterStyle={mediaStyle}
      readyTimeoutMs={policy.readyTimeoutMs} allowAudio={!policy.forceMuted} emit={emit}
      context={{ role: policy.role, visible, reducedMotion, locale: i18n.language }} />;
  }

  return (
    <div ref={surface} className="webui-media-surface" data-testid="webui-media-surface" data-variant={p.kind}
      aria-hidden="true" style={{ opacity: p.opacity }}>
      <div className="webui-media-surface__layer" style={layerStyle}>{content}</div>
      {p.dim > 0 ? <div className="webui-media-surface__dim" data-testid="webui-media-surface-dim"
        style={{ opacity: p.dim }} /> : null}
    </div>
  );
}
