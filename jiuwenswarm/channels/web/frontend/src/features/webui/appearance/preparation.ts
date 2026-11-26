/**
 * 外观准备（Gateway S_09 第 3.4、4、5 节）：向声明了 `application.appearance` 的包发送 `ui.ready`，
 * 等待回执期间（至多 3 秒）暂存收到的快照，回执到达后一次应用；维护首帧记录与品牌位置使用的标识。
 */

import type { WebUIRuntime } from '../runtime/types.ts';
import type { WebUIDispatchReceipt } from '../transport/types.ts';
import { APPEARANCE_EXPECTED_KEY, APPEARANCE_IDENTITY_KEY, readItem, writeItem } from '../splash/launch.ts';
import { validateIdentity, type WebUIIdentity } from './appearance.ts';

export const APPEARANCE_SLOT_ID = 'application.appearance';
export const APPEARANCE_INSTANCE_KEY = 'main';
/** Longest wait for appearance providers: at the gate, and for snapshots held back after a ready. */
export const APPEARANCE_READY_MS = 3_000;

export interface AppearanceTimers {
  set(callback: () => void, ms: number): unknown;
  clear(handle: unknown): void;
}

export interface AppearancePreparation {
  /** Settles with the receipt of this page's first appearance ready; null when the request failed. */
  ready(): Promise<WebUIDispatchReceipt | null>;
  /** The gate stops waiting: records are written and brand spots follow the live snapshot from now on. */
  finish(): void;
  /** Identity for the host brand spots: the last record until preparation ends, then the live snapshot's. */
  getIdentity(): WebUIIdentity | null;
  subscribe(listener: () => void): () => void;
  dispose(): void;
}

export interface AppearancePreparationOptions {
  runtime: WebUIRuntime;
  timers?: AppearanceTimers;
}

const defaultTimers: AppearanceTimers = {
  set: (callback, ms) => globalThis.setTimeout(callback, ms),
  clear: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/** The identity record written by an earlier page; invalid or missing records are null. */
export function readIdentityRecord(): WebUIIdentity | null {
  const raw = readItem(APPEARANCE_IDENTITY_KEY);
  if (!raw) return null;
  try {
    return validateIdentity(JSON.parse(raw));
  } catch {
    return null;
  }
}

function sameIdentity(left: WebUIIdentity | null, right: WebUIIdentity | null): boolean {
  return left?.name === right?.name && left?.logo === right?.logo && left?.favicon === right?.favicon;
}

export function createAppearancePreparation(options: AppearancePreparationOptions): AppearancePreparation {
  const { runtime } = options;
  const timers = options.timers ?? defaultTimers;
  const listeners = new Set<() => void>();
  // Snapshots stay back while a ready is answered, at most APPEARANCE_READY_MS per ready.
  const holds: Array<{ release: () => void; handle: unknown }> = [];
  const record = readIdentityRecord();
  let settled = false;
  let disposed = false;
  let answered = false;
  let identity = currentIdentity();
  let resolveReady: (receipt: WebUIDispatchReceipt | null) => void = () => undefined;
  const firstReady = new Promise<WebUIDispatchReceipt | null>((resolve) => { resolveReady = resolve; });

  function currentIdentity(): WebUIIdentity | null {
    return runtime.getAppearance()?.parts.identity?.value ?? (settled ? null : record);
  }

  function refresh(): void {
    const next = currentIdentity();
    if (settled) {
      // The record follows every change after preparation, and is removed when no package provides an identity.
      writeItem(APPEARANCE_IDENTITY_KEY, next ? JSON.stringify(next) : null);
    }
    if (sameIdentity(next, identity)) return;
    identity = next;
    for (const listener of [...listeners]) listener();
  }

  function settle(): void {
    if (settled || disposed) return;
    settled = true;
    refresh();
  }

  function releaseOldest(): void {
    const hold = holds.shift();
    if (!hold) return;
    timers.clear(hold.handle);
    hold.release();
  }

  const offAppearance = runtime.subscribeAppearance(refresh);
  const mount = runtime.mountSlot('', APPEARANCE_SLOT_ID, {
    instanceKey: APPEARANCE_INSTANCE_KEY,
    onReadyStart() {
      const release = runtime.holdAppearance();
      const entry = {
        release,
        handle: timers.set(() => {
          const index = holds.indexOf(entry);
          if (index >= 0) holds.splice(index, 1);
          release();
        }, APPEARANCE_READY_MS),
      };
      holds.push(entry);
    },
    onReadySettled(receipt, packageId) {
      releaseOldest();
      // A ready for one changed package says nothing about the others.
      if (disposed || packageId !== undefined) return;
      // Every answer of all providers corrects the first-paint expectation for the next page (S_09 section 5).
      if (receipt) writeItem(APPEARANCE_EXPECTED_KEY, receipt.accepted ? '1' : '0');
      if (!answered) {
        answered = true;
        resolveReady(receipt);
      }
      settle();
    },
  });
  // Sent now when connected, otherwise when the connection comes up; the runtime repeats it after reconnects.
  void mount.notifyReady();

  return {
    ready: () => firstReady,
    finish() {
      if (disposed) return;
      if (!answered) writeItem(APPEARANCE_EXPECTED_KEY, '0');
      settle();
    },
    getIdentity: () => identity,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      while (holds.length) releaseOldest();
      offAppearance();
      mount.release();
      listeners.clear();
      resolveReady(null);
    },
  };
}
