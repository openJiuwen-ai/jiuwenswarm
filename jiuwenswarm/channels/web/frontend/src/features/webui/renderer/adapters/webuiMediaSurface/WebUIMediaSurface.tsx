/**
 * 媒体容器：按 kind 显示图片、视频或沙箱页面，失败、超时或减少动态时回退到封面。
 * 静音、就绪超时与角色来自宿主策略；素材地址是平台路由，直接使用以保留分段读取和页面内相对引用。
 * 失败的内容在浏览器连接恢复后重新加载一次；有画面（图片加载、视频首帧、页面就绪或封面加载）时才向宿主报告在场。
 */

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, type CSSProperties } from 'react';
import { useTranslation } from 'react-i18next';
import type { WebUIAdapterProps } from '../../types.ts';
import {
  prefersReducedMotion, REDUCED_MOTION_QUERY, useMediaSurfacePolicy, type MediaSurfaceEvent,
} from './hostPolicy.ts';
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

function subscribeReducedMotion(listener: () => void): () => void {
  const query = window.matchMedia?.(REDUCED_MOTION_QUERY);
  query?.addEventListener?.('change', listener);
  return () => query?.removeEventListener?.('change', listener);
}
const getReducedMotion = prefersReducedMotion;
function subscribeVisibility(listener: () => void): () => void {
  document.addEventListener('visibilitychange', listener);
  return () => document.removeEventListener('visibilitychange', listener);
}
const getVisible = (): boolean => document.visibilityState !== 'hidden';
const serverFalse = (): boolean => false;
const serverTrue = (): boolean => true;

type Emit = (event: MediaSurfaceEvent) => void;
/** Whether the surface shows a picture: a loaded image, a video frame, a ready page or a loaded poster. */
type ReportShown = (shown: boolean) => void;

interface PosterProps {
  src?: string;
  style: CSSProperties;
  onShown: ReportShown;
  /** Shown in place of failed media: if it cannot show either, the surface is empty. */
  fallback?: boolean;
  /** Set when the poster is the surface's only content, so a reconnect can retry it. */
  onFailed?: () => void;
}

function Poster({ src, style, onShown, fallback = false, onFailed }: PosterProps) {
  // Remember which address failed: a mounted background may receive a new poster later (S_07).
  const [failed, setFailed] = useState<string | null>(null);
  const missing = !src || failed === src;
  useEffect(() => {
    if (missing && fallback) onShown(false);
  }, [missing, fallback, onShown]);
  if (missing) return null;
  return <img className="webui-media-surface__media" data-testid="webui-media-surface-poster" src={src} alt=""
    draggable={false} style={style} onLoad={() => onShown(true)} onError={() => { setFailed(src); onFailed?.(); }} />;
}

interface SurfaceImageProps {
  src: string;
  poster?: string;
  style: CSSProperties;
  emit: Emit;
  onShown: ReportShown;
  onFailed: () => void;
}

function SurfaceImage({ src, poster, style, emit, onShown, onFailed }: SurfaceImageProps) {
  const [failed, setFailed] = useState(false);
  if (failed) return <Poster src={poster} style={style} onShown={onShown} fallback />;
  return <img className="webui-media-surface__media" data-testid="webui-media-surface-image" src={src} alt=""
    draggable={false} style={style} onLoad={() => { emit('ready'); onShown(true); }}
    onError={() => { emit('error'); setFailed(true); onFailed(); }} />;
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
  onShown: ReportShown;
  onFailed: () => void;
}

function SurfaceVideo({ src, poster, style, loop, muted, autoPlay, visible, emit, onShown, onFailed }: SurfaceVideoProps) {
  const video = useRef<HTMLVideoElement>(null);
  const [failed, setFailed] = useState(false);
  const started = useRef(false);

  useEffect(() => {
    // React does not reflect `muted` reliably; autoplay policies read the property.
    if (video.current) video.current.muted = muted;
  }, [muted]);

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
    void element.play()?.catch(retryMuted);
  }, [autoPlay, visible]);

  if (failed) return <Poster src={poster} style={style} onShown={onShown} fallback />;
  // A decoded first frame is a picture even while the video is paused.
  return <video ref={video} className="webui-media-surface__media" data-testid="webui-media-surface-video"
    src={src} poster={poster} style={style} loop={loop} muted={muted} autoPlay={autoPlay && visible} playsInline
    preload="auto" controls={false} disablePictureInPicture disableRemotePlayback tabIndex={-1}
    onPlaying={() => { if (!started.current) { started.current = true; emit('ready'); } }}
    onLoadedData={() => onShown(true)} onTimeUpdate={() => emit('heartbeat')} onEnded={() => emit('complete')}
    onError={() => { emit('error'); setFailed(true); onFailed(); }} />;
}

interface SurfaceFrameProps {
  src: string;
  title: string;
  poster?: string;
  posterStyle: CSSProperties;
  context: SurfaceContext;
  /** Fall back to the poster when the page is not ready in time; omitted when the host owns timing. */
  readyTimeoutMs?: number;
  /** Delegate autoplay with sound; withheld for silent surfaces so the page cannot start audio. */
  allowAudio: boolean;
  emit: Emit;
  onShown: ReportShown;
  onFailed: () => void;
}

function SurfaceFrame({
  src, title, poster, posterStyle, context, readyTimeoutMs, allowAudio, emit, onShown, onFailed,
}: SurfaceFrameProps) {
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
        onShown(true);
        post('start');
      } else if (surfaceEvent === 'error') {
        setPhase('failed');
      }
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [token]);

  useEffect(() => {
    if (phase === 'failed') onFailed();
  }, [phase, onFailed]);

  useEffect(() => {
    if (phase !== 'loading' || readyTimeoutMs === undefined) return undefined;
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

  if (phase === 'failed') return <Poster src={poster} style={posterStyle} onShown={onShown} fallback />;
  // Transparent rather than hidden before ready, so readiness does not depend on whether an engine
  // throttles hidden cross-origin frames (Chromium 157 does not; see Gateway F_10).
  return (
    <>
      <iframe ref={frame} className="webui-media-surface__media" data-testid="webui-media-surface-frame"
        data-variant={phase} src={withSurfaceToken(src, token)} title={title} sandbox="allow-scripts"
        allow={allowAudio ? 'autoplay' : ''} referrerPolicy="no-referrer" tabIndex={-1}
        style={{ opacity: phase === 'ready' ? 1 : 0 }} onLoad={() => post('start')} />
      {phase === 'loading' ? <Poster src={poster} style={posterStyle} onShown={onShown} /> : null}
    </>
  );
}

export function WebUIMediaSurface({ node, context: adapterContext }: WebUIAdapterProps) {
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

  // Present while a picture shows; content still loading after a switch keeps the previous state (S_07).
  const [shown, setShown] = useState(false);
  useEffect(() => {
    if (!shown || !onPresence) return undefined;
    onPresence(true);
    return () => onPresence(false);
  }, [shown, onPresence]);

  const mediaStyle: CSSProperties = { objectFit: p.fit, objectPosition: p.position };

  const posterOnly = policy.honorReducedMotion && p.kind !== 'image' && reducedMotion && !!p.poster;
  const identity = (posterOnly ? p.poster : p.src) ?? '';

  // A failed address stays failed until the browser reconnects; then it loads once more. Healthy content
  // is not reloaded by a reconnect (S_07 invariant 6).
  const failedIdentity = useRef<string | null>(null);
  const [retries, setRetries] = useState(0);
  const onFailed = useCallback(() => { failedIdentity.current = identity; }, [identity]);
  const { connected } = adapterContext;
  useEffect(() => {
    if (!connected || failedIdentity.current === null || failedIdentity.current !== identity) return;
    failedIdentity.current = null;
    setRetries((count) => count + 1);
  }, [connected]);
  const key = `${identity}#${retries}`;

  let content;
  if (p.kind === 'image') {
    content = <SurfaceImage key={key} src={p.src} poster={p.poster} style={mediaStyle} emit={emit} onShown={setShown}
      onFailed={onFailed} />;
  } else if (posterOnly) {
    content = <Poster key={key} src={p.poster} style={mediaStyle} onShown={setShown} fallback onFailed={onFailed} />;
  } else if (p.kind === 'video') {
    content = <SurfaceVideo key={key} src={p.src} poster={p.poster} style={mediaStyle} loop={policy.allowLoop && p.loop}
      muted={policy.forceMuted || p.muted} autoPlay={!(policy.honorReducedMotion && reducedMotion)} visible={visible}
      emit={emit} onShown={setShown} onFailed={onFailed} />;
  } else {
    content = <SurfaceFrame key={key} src={p.src} title={p.title} poster={p.poster} posterStyle={mediaStyle}
      readyTimeoutMs={policy.readyTimeoutMs} allowAudio={!policy.forceMuted} emit={emit} onShown={setShown}
      onFailed={onFailed} context={{ role: policy.role, visible, reducedMotion, locale: i18n.language }} />;
  }

  return (
    <div className="webui-media-surface" data-testid="webui-media-surface" data-variant={p.kind}
      aria-hidden="true" style={{ opacity: p.opacity }}>
      <div className="webui-media-surface__layer">{content}</div>
      {/* Blurs the media behind it in place: no layout change for the page, no transparent rim (S_07). */}
      {p.blur > 0 ? <div className="webui-media-surface__blur" data-testid="webui-media-surface-blur"
        style={{ '--webui-media-surface-blur': `${p.blur}px` } as CSSProperties} /> : null}
      {p.dim > 0 ? <div className="webui-media-surface__dim" data-testid="webui-media-surface-dim"
        style={{ opacity: p.dim }} /> : null}
    </div>
  );
}
