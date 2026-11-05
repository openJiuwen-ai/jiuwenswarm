/**
 * 启动门控（Gateway S_09 第 5 节）：门控开放前显示中性层与开场，`#root` 不接收输入，登录入口不弹出。
 * 开场层挂在 `#root` 之外；结束后记录本次启动已完成并开放界面。
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { MediaSurfacePolicyContext, SPLASH_SURFACE_POLICY } from '../renderer/adapters/webuiMediaSurface/hostPolicy.ts';
import type { WebUIRendererRegistry } from '../renderer/types.ts';
import type { WebUIMountOptions, WebUIRuntime } from '../runtime/types.ts';
import {
  SPLASH_INSTANCE_KEY,
  SPLASH_SLOT_ID,
  createSplashController,
  type SplashController,
  type SplashTimers,
} from '../splash/controller.ts';
import { APPEARANCE_PROVIDER_KEY, readItem, type SplashHint, type SplashLaunch } from '../splash/launch.ts';
import { WebUIHost } from './WebUIHost.tsx';
import { useOptionalWebUIAppContext } from './WebUIProvider.tsx';
import { SplashGateContext } from './splashGateContext.ts';
import './WebUISplashGate.css';

/** The first-paint layer written into index.html. */
export const STATIC_NEUTRAL_ID = 'workswarm-neutral';
export const SKIP_HOLD_MS = 800;
// The controller sends the targeted ready; the display host must not send another.
const DISPLAY_ONLY: WebUIMountOptions = { notifyReady: false };
const RING = 2 * Math.PI * 15;
const noSubscribe = (): (() => void) => () => undefined;

export interface WebUISplashGateProps {
  children?: ReactNode;
  launch: SplashLaunch;
  hint(): Promise<SplashHint>;
  requestProviders(): Promise<unknown>;
  /** 测试或特殊宿主的注入点；生产环境由 WebUIProvider 提供。 */
  runtime?: WebUIRuntime;
  registry?: WebUIRendererRegistry;
  timers?: SplashTimers;
}

function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
  } catch {
    return false;
  }
}

function SkipHint({ onSkip }: { onSkip: () => void }) {
  const { t } = useTranslation();
  const [progress, setProgress] = useState(0);
  const hold = useRef<number | null>(null);
  const skip = useRef(onSkip);
  skip.current = onSkip;

  const cancel = useCallback(() => {
    if (hold.current !== null) cancelAnimationFrame(hold.current);
    hold.current = null;
    setProgress(0);
  }, []);
  const begin = useCallback(() => {
    if (hold.current !== null) return;
    const started = performance.now();
    const step = () => {
      const value = Math.min(1, (performance.now() - started) / SKIP_HOLD_MS);
      setProgress(value);
      if (value >= 1) {
        hold.current = null;
        skip.current();
        return;
      }
      hold.current = requestAnimationFrame(step);
    };
    hold.current = requestAnimationFrame(step);
  }, []);

  useEffect(() => {
    const down = (event: KeyboardEvent) => {
      if (event.code !== 'Space') return;
      event.preventDefault();
      if (!event.repeat) begin();
    };
    const up = (event: KeyboardEvent) => {
      if (event.code !== 'Space') return;
      event.preventDefault();
      cancel();
    };
    window.addEventListener('keydown', down, true);
    window.addEventListener('keyup', up, true);
    window.addEventListener('blur', cancel);
    return () => {
      window.removeEventListener('keydown', down, true);
      window.removeEventListener('keyup', up, true);
      window.removeEventListener('blur', cancel);
      cancel();
    };
  }, [begin, cancel]);

  return (
    <button type="button" className="webui-splash__skip" data-testid="webui-splash-skip" tabIndex={-1}
      aria-label={t('webuiSplash.holdToSkip')} onPointerDown={(event) => { event.preventDefault(); begin(); }}
      onPointerUp={cancel} onPointerLeave={cancel} onPointerCancel={cancel}>
      <svg className="webui-splash__ring" viewBox="0 0 36 36" aria-hidden="true">
        <circle cx="18" cy="18" r="15" className="webui-splash__ring-track" />
        <circle cx="18" cy="18" r="15" className="webui-splash__ring-value" data-testid="webui-splash-skip-progress"
          strokeDasharray={RING} strokeDashoffset={RING * (1 - progress)} />
      </svg>
      <span data-testid="webui-splash-skip-label">{t('webuiSplash.holdToSkip')}</span>
    </button>
  );
}

export function WebUISplashGate({
  children, launch, hint, requestProviders, runtime: runtimeOverride, registry: registryOverride, timers,
}: WebUISplashGateProps) {
  const context = useOptionalWebUIAppContext();
  const runtime = runtimeOverride ?? context?.runtime;
  const registry = registryOverride ?? context?.registry;
  // The look continues the first-paint layer when index.html showed one.
  const [neutralAtBoot] = useState(() => typeof document !== 'undefined' && !!document.getElementById(STATIC_NEUTRAL_ID));
  const options = useRef({ launch, hint, requestProviders, timers });
  options.current = { launch, hint, requestProviders, timers };
  // Created in a layout effect so a remount starts a fresh controller before the first paint.
  const [controller, setController] = useState<SplashController | null>(null);
  useLayoutEffect(() => {
    if (!runtime) return undefined;
    const created = createSplashController({
      runtime, ...options.current,
      preferredProvider: readItem(APPEARANCE_PROVIDER_KEY),
      reducedMotion: prefersReducedMotion,
    });
    setController(created);
    void created.start();
    return () => created.dispose();
  }, [runtime]);
  const state = useSyncExternalStore(
    controller ? controller.subscribe : noSubscribe,
    () => controller?.getState() ?? null,
    () => controller?.getState() ?? null,
  );
  // Closed until the controller decides; without a runtime there is nothing to wait for.
  const open = !runtime || state?.phase === 'open';

  useLayoutEffect(() => {
    document.getElementById(STATIC_NEUTRAL_ID)?.remove();
  }, []);

  useLayoutEffect(() => {
    const root = document.getElementById('root');
    if (!root) return undefined;
    if (open) {
      root.removeAttribute('inert');
      return undefined;
    }
    root.setAttribute('inert', '');
    return () => root.removeAttribute('inert');
  }, [open]);

  const policy = useMemo(
    () => ({ ...SPLASH_SURFACE_POLICY, onSurfaceEvent: (event: Parameters<SplashController['surfaceEvent']>[0]) => controller?.surfaceEvent(event) }),
    [controller],
  );
  const provider = state?.phase === 'playing' ? state.provider : null;
  const look = neutralAtBoot || (state && state.phase !== 'deciding') ? 'neutral' : 'plain';

  return (
    <SplashGateContext.Provider value={open}>
      {children}
      {!open && typeof document !== 'undefined' && createPortal(
        <div className="webui-splash" data-testid="webui-splash" data-variant={state?.phase} data-look={look}>
          {provider ? (
            <MediaSurfacePolicyContext.Provider value={policy}>
              <WebUIHost slotId={SPLASH_SLOT_ID} packageId={provider.packageId} instanceKey={SPLASH_INSTANCE_KEY}
                mountOptions={DISPLAY_ONLY} runtime={runtime} registry={registry}
                className="webui-splash__surface" instanceClassName="webui-splash__instance" />
              {provider.skippable ? <SkipHint onSkip={() => controller?.skip()} /> : null}
            </MediaSurfacePolicyContext.Provider>
          ) : look === 'neutral' ? (
            <div className="webui-splash__neutral" data-testid="webui-splash-neutral" aria-hidden="true">
              <img className="webui-splash__logo" src="/logo.svg" alt="" draggable={false} />
              <div className="webui-splash__name">WorkSwarm</div>
              <div className="webui-splash__spinner" />
            </div>
          ) : null}
        </div>,
        document.body,
      )}
    </SplashGateContext.Provider>
  );
}
