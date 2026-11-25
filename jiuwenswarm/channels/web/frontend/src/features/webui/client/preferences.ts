import { useSyncExternalStore } from 'react';
import type { WebUIPreferenceBinding as PreferenceBinding } from '../contracts/wire.ts';
import type { WebUINormalizedRenderFrame } from '../contracts/normalized.ts';

const PREFIX = 'jiuwenswarm.webui.preference.v1:';
const listeners = new Set<() => void>();
let version = 0;

function changed() {
  version++;
  listeners.forEach((listener) => listener());
}

function storageChanged(event: StorageEvent) {
  if (event.storageArea === window.localStorage && (event.key === null || event.key.startsWith(PREFIX))) changed();
}

export function subscribePreferences(listener: () => void): () => void {
  if (!listeners.size && typeof window !== 'undefined') window.addEventListener('storage', storageChanged);
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    if (!listeners.size && typeof window !== 'undefined') window.removeEventListener('storage', storageChanged);
  };
}

export const preferenceVersion = () => version;

function storageKey(packageId: string | undefined, key: string): string {
  if (!packageId) throw new Error('A client preference requires a trusted package identity');
  return PREFIX + JSON.stringify([packageId, key]);
}

export function readPreference(packageId: string | undefined, binding: PreferenceBinding): boolean {
  const value = window.localStorage.getItem(storageKey(packageId, binding.key));
  if (value === null) return binding.default;
  if (value === 'true') return true;
  if (value === 'false') return false;
  throw new Error('Invalid stored client preference');
}

export function readInstanceVisibility(frame: WebUINormalizedRenderFrame): boolean {
  return frame.visibleWhen ? readPreference(frame.target.packageId, frame.visibleWhen) : true;
}

export function writePreference(packageId: string | undefined, binding: PreferenceBinding, value: boolean): void {
  window.localStorage.setItem(storageKey(packageId, binding.key), String(value));
  changed();
}

export function useClientPreference(packageId: string | undefined, binding: PreferenceBinding | undefined): boolean {
  return useSyncExternalStore(
    subscribePreferences,
    () => (binding ? readPreference(packageId, binding) : true),
    () => (binding ? binding.default : true),
  );
}
