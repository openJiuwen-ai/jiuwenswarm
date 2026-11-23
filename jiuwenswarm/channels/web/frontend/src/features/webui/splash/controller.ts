/**
 * 开场队列控制（Gateway S_09 第 5 节）：判定是否等待、外观准备、依次准备与播放提供者、计时与跳过、结束通知。
 * 每个等待都有时限，门控总会开放；结束先在本地关闭，再通知提供者。
 */

import { isRecord } from '../protocol/utils.ts';
import type { MediaSurfaceEvent } from '../renderer/adapters/webuiMediaSurface/hostPolicy.ts';
import type { WebUIRuntime } from '../runtime/types.ts';
import type { WebUIDispatchReceipt } from '../transport/types.ts';
import { APPEARANCE_READY_MS, type AppearancePreparation } from '../appearance/preparation.ts';
import {
  APPEARANCE_EXPECTED_KEY, SPLASH_EXPECTED_KEY, SPLASH_PLAYED_KEY, UNKNOWN_HINT, readItem, writeItem,
  type SplashLaunch, type StartupHint,
} from './launch.ts';

export const SPLASH_SLOT_ID = 'application.splash';
export const SPLASH_INSTANCE_KEY = 'main';
/** Downstream preview request (Gateway S_09 section 3.3). */
export const SPLASH_PLAY_EVENT = 'webui.splash.play';
const MAX_SPLASH_MS = 60_000;

export const SPLASH_TIMEOUTS = {
  hint: 1_500,
  /** Waiting for appearance providers (Gateway S_09 section 5). */
  appearance: APPEARANCE_READY_MS,
  /** Waiting for the Gateway when the Web server reported candidates. */
  connectHinted: 30_000,
  /** Waiting for the Gateway when no hint was available. */
  connectUnknown: 3_000,
  answer: 5_000,
  mediaReady: 10_000,
  heartbeat: 5_000,
  image: 3_000,
} as const;

export interface SplashProvider {
  packageId: string;
  skippable: boolean;
  maxDurationMs: number;
}

export type SplashPhase = 'deciding' | 'preparing' | 'playing' | 'open';
export type SplashCloseReason = 'complete' | 'skip' | 'timeout' | 'error' | 'disabled' | 'removed';

export interface SplashState {
  phase: SplashPhase;
  provider: SplashProvider | null;
  instanceKey: string | null;
  /** The hint named splash candidates or appearance providers: while deciding, the gate shows the neutral backdrop. */
  expected: boolean;
}

export interface SplashTimers {
  set(callback: () => void, ms: number): unknown;
  clear(handle: unknown): void;
}

export interface SplashControllerOptions {
  runtime: WebUIRuntime;
  launch: SplashLaunch;
  hint(): Promise<StartupHint>;
  requestProviders(): Promise<unknown>;
  /** Appearance providers to wait for before the splash decision; every page load waits, at most 3 seconds. */
  appearance?: AppearancePreparation;
  /** The current colors provider plays first unless the user saved an order; asked when the queue is built. */
  preferredProvider?: () => string | null;
  reducedMotion(): boolean;
  timers?: SplashTimers;
}

export interface SplashController {
  getState(): SplashState;
  subscribe(listener: () => void): () => void;
  start(): Promise<void>;
  /** Play one provider again after the gate opened (Gateway S_09 section 3.3); false when ignored. */
  preview(provider: SplashProvider): Promise<boolean>;
  surfaceEvent(event: MediaSurfaceEvent): void;
  skip(): void;
  dispose(): void;
}

const defaultTimers: SplashTimers = {
  set: (callback, ms) => globalThis.setTimeout(callback, ms),
  clear: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};


/** Read the Gateway provider list; malformed entries are dropped. */
export function parseProviders(value: unknown): SplashProvider[] {
  if (!isRecord(value) || !Array.isArray(value.providers)) return [];
  const seen = new Set<string>();
  const providers: SplashProvider[] = [];
  for (const item of value.providers) {
    if (!isRecord(item) || typeof item.package_id !== 'string' || !item.package_id || seen.has(item.package_id)
      || typeof item.skippable !== 'boolean' || typeof item.max_duration_ms !== 'number'
      || !Number.isInteger(item.max_duration_ms) || item.max_duration_ms <= 0) continue;
    seen.add(item.package_id);
    providers.push({
      packageId: item.package_id,
      skippable: item.skippable,
      maxDurationMs: Math.min(item.max_duration_ms, MAX_SPLASH_MS),
    });
  }
  return providers;
}

/** Read a `webui.splash.play` preview request; null when malformed. */
export function parseSplashPlay(value: unknown): SplashProvider | null {
  return parseProviders({ providers: [value] })[0] ?? null;
}

/** Whether Gateway has a saved splash order (Gateway S_10 section 4). */
export function parseCustomized(value: unknown): boolean {
  return isRecord(value) && value.customized === true;
}

/**
 * The splash order (Gateway S_10 section 4): a saved order is final; otherwise the appearance provider goes first.
 */
export function orderProviders<T extends { packageId: string }>(
  providers: readonly T[], preferred?: string | null, customized = false,
): T[] {
  if (customized || !preferred) return [...providers];
  const first = providers.filter((provider) => provider.packageId === preferred);
  return [...first, ...providers.filter((provider) => provider.packageId !== preferred)];
}

export function createSplashController(options: SplashControllerOptions): SplashController {
  const { runtime, launch } = options;
  const timers = options.timers ?? defaultTimers;
  const listeners = new Set<() => void>();
  let expected = false;
  let state: SplashState = { phase: 'deciding', provider: null, instanceKey: null, expected };
  let disposed = false;
  let started = false;
  let onSurface: ((event: MediaSurfaceEvent) => void) | null = null;
  let onSkip: (() => void) | null = null;
  let stopPlayback: (() => void) | null = null;

  function set(next: Omit<SplashState, 'expected'>): void {
    state = { ...next, expected };
    for (const listener of [...listeners]) listener();
  }

  function delay<T>(promise: Promise<T>, ms: number, fallback: T): Promise<T> {
    return new Promise((resolve) => {
      const handle = timers.set(() => resolve(fallback), ms);
      promise.then(
        (value) => { timers.clear(handle); resolve(value); },
        () => { timers.clear(handle); resolve(fallback); },
      );
    });
  }

  function waitConnected(ms: number): Promise<boolean> {
    if (runtime.getConnectionState() === 'connected') return Promise.resolve(true);
    return new Promise((resolve) => {
      const finish = (connected: boolean) => { off(); timers.clear(handle); resolve(connected); };
      const off = runtime.subscribe(() => {
        if (runtime.getConnectionState() === 'connected') finish(true);
      });
      const handle = timers.set(() => finish(false), ms);
    });
  }

  function open(): void {
    if (state.phase === 'open') return;
    launch.markCompleted();
    set({ phase: 'open', provider: null, instanceKey: null });
  }

  function splashInstance(provider: SplashProvider): string | null {
    const scope = runtime.getScopeSnapshot('', SPLASH_SLOT_ID);
    return scope.instanceKeys.find((key) => runtime.getInstance(key)?.target.packageId === provider.packageId) ?? null;
  }

  /** Resolve with the close reason, or null when the instance disappeared on its own. */
  function playback(provider: SplashProvider, instanceKey: string): Promise<SplashCloseReason | null> {
    const kind = (runtime.getInstance(instanceKey)?.frame?.tree.props as { kind?: unknown } | undefined)?.kind;
    return new Promise((resolve) => {
      const handles = new Map<string, unknown>();
      let ready = false;
      let done = false;
      const arm = (name: string, ms: number, reason: SplashCloseReason) => {
        if (handles.has(name)) timers.clear(handles.get(name));
        handles.set(name, timers.set(() => finish(reason), ms));
      };
      const offInstance = runtime.subscribeInstance(instanceKey, () => {
        if (!runtime.getInstance(instanceKey)) finish(null);
      });
      function finish(reason: SplashCloseReason | null): void {
        if (done) return;
        done = true;
        for (const handle of handles.values()) timers.clear(handle);
        offInstance();
        onSurface = null;
        onSkip = null;
        stopPlayback = null;
        resolve(reason);
      }
      onSurface = (event) => {
        if (event === 'complete') finish('complete');
        else if (event === 'error') finish('error');
        else if (event === 'ready' && !ready) {
          ready = true;
          if (handles.has('ready')) timers.clear(handles.get('ready'));
          handles.delete('ready');
          if (kind === 'image') {
            arm('limit', Math.min(SPLASH_TIMEOUTS.image, provider.maxDurationMs), 'complete');
          } else {
            arm('limit', provider.maxDurationMs, 'timeout');
            arm('heartbeat', SPLASH_TIMEOUTS.heartbeat, 'timeout');
          }
        } else if (event === 'heartbeat' && ready && kind !== 'image') {
          arm('heartbeat', SPLASH_TIMEOUTS.heartbeat, 'timeout');
        }
      };
      onSkip = () => { if (provider.skippable) finish('skip'); };
      stopPlayback = () => finish(null);
      arm('ready', SPLASH_TIMEOUTS.mediaReady, 'timeout');
    });
  }

  async function play(provider: SplashProvider, preview = false): Promise<void> {
    set({ phase: 'preparing', provider, instanceKey: null });
    let settle: (receipt: WebUIDispatchReceipt | null) => void = () => undefined;
    const answered = new Promise<WebUIDispatchReceipt | null>((resolve) => { settle = resolve; });
    const handle = runtime.mountSlot('', SPLASH_SLOT_ID, {
      packageId: provider.packageId,
      instanceKey: SPLASH_INSTANCE_KEY,
      readyOnce: true,
      // A preview is never a first visit (Gateway S_09 section 3.3).
      readyPayload: {
        launch_id: launch.id, first_visit: preview ? false : launch.firstVisit,
        reduced_motion: options.reducedMotion(), preview,
      },
      onReadySettled: (receipt) => settle(receipt),
    });
    try {
      void handle.notifyReady();
      const receipt = await delay(answered, SPLASH_TIMEOUTS.answer, null);
      const instanceKey = !disposed && receipt?.status === 'playing' ? splashInstance(provider) : null;
      if (!instanceKey) return;
      set({ phase: 'playing', provider, instanceKey });
      const reason = await playback(provider, instanceKey);
      // Close locally first so sound and motion stop before the package hears about it.
      set({ phase: 'preparing', provider: null, instanceKey: null });
      // A startup splash that was watched or skipped ends the device's first visit (Gateway S_09
      // first_visit); one that timed out, failed or was taken back by its provider does not.
      if (!preview && (reason === 'complete' || reason === 'skip')) writeItem(SPLASH_PLAYED_KEY, '1');
      if (reason && !disposed) void runtime.notifyClosed(instanceKey, reason);
    } finally {
      handle.release();
    }
  }

  /** Stop waiting for appearance providers; snapshots arriving later still apply. */
  function finishAppearance(): void {
    if (!disposed) options.appearance?.finish();
  }

  async function start(): Promise<void> {
    if (started || disposed) return;
    started = true;
    const { appearance } = options;
    // A reload of a completed launch opens at once unless the last page had appearance providers to wait for;
    // the preparation still asks the Gateway and corrects the record for the next page.
    if (launch.completed && (!appearance || readItem(APPEARANCE_EXPECTED_KEY) !== '1')) {
      finishAppearance();
      open();
      return;
    }
    // Waiting only for appearance counts from here, the hint and the connection included.
    let deadline: unknown = null;
    const appearanceDeadline = new Promise<null>((resolve) => {
      deadline = timers.set(() => resolve(null), SPLASH_TIMEOUTS.appearance);
    });
    try {
      await decide(appearanceDeadline);
    } finally {
      timers.clear(deadline);
    }
  }

  async function decide(appearanceDeadline: Promise<null>): Promise<void> {
    const { appearance } = options;
    const hint = await delay(Promise.resolve().then(options.hint), SPLASH_TIMEOUTS.hint, UNKNOWN_HINT);
    if (disposed) return;
    // A completed launch plays no splash; the appearance is still prepared on every page load.
    const splash = launch.completed ? null : hint.splash;
    const waitAppearance = hint.appearance !== false;
    if (splash && splash !== 'unknown') writeItem(SPLASH_EXPECTED_KEY, splash === 'candidates' ? '1' : '0');
    if (splash === 'candidates' || (appearance && hint.appearance === true)) {
      // Waiting for the Gateway can take a while; show the splash backdrop rather than a blank page meanwhile.
      expected = true;
      set(state);
    }
    if (splash !== 'candidates' && splash !== 'unknown') {
      if (waitAppearance && appearance) await Promise.race([appearance.ready(), appearanceDeadline]);
      if (disposed) return;
      finishAppearance();
      open();
      // After a "none" hint the gate stays open; the Gateway's answer only corrects the hint for the next start.
      if (splash === 'none') await refreshHint();
      return;
    }
    const connected = await waitConnected(splash === 'candidates' ? SPLASH_TIMEOUTS.connectHinted : SPLASH_TIMEOUTS.connectUnknown);
    if (disposed) return;
    if (!connected) {
      finishAppearance();
      open();
      return;
    }
    if (waitAppearance && appearance) await delay(appearance.ready(), SPLASH_TIMEOUTS.appearance, null);
    if (disposed) return;
    finishAppearance();
    for (const provider of await askProviders()) {
      if (disposed) return;
      await play(provider);
    }
    if (!disposed) open();
  }

  async function askProviders(): Promise<SplashProvider[]> {
    const answer = await delay(Promise.resolve().then(options.requestProviders), SPLASH_TIMEOUTS.answer, null);
    const providers = orderProviders(parseProviders(answer), options.preferredProvider?.(), parseCustomized(answer));
    if (answer !== null) writeItem(SPLASH_EXPECTED_KEY, providers.length ? '1' : '0');
    return providers;
  }

  /** After a "none" hint, ask the Gateway once so a splash installed meanwhile is expected next time. */
  async function refreshHint(): Promise<void> {
    if (!(await waitConnected(SPLASH_TIMEOUTS.connectUnknown)) || disposed) return;
    await askProviders();
  }

  async function preview(provider: SplashProvider): Promise<boolean> {
    // Previews only run on an open gate with nothing playing, one at a time.
    if (disposed || state.phase !== 'open') return false;
    try {
      await play(provider, true);
    } finally {
      // Back to open without completing the launch again.
      if (!disposed) set({ phase: 'open', provider: null, instanceKey: null });
    }
    return true;
  }

  return {
    getState: () => state,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    start,
    preview,
    surfaceEvent: (event) => onSurface?.(event),
    skip: () => onSkip?.(),
    dispose() {
      disposed = true;
      stopPlayback?.();
      listeners.clear();
    },
  };
}
