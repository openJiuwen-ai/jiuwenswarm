import { useSyncExternalStore } from 'react';
import { parseConfigBoolean, SETTINGS_CONFIG_CHANGED_EVENT } from '../settings/services/settingsContract';

// Navigation stays hidden until the first confirmed server snapshot arrives.
let enabled = false;
let revision = 0;
const listeners = new Set<() => void>();
const MEMORY_SWITCH_EVENT = 'jiuwenswarm.memory.portal.switch';

export function getMemoryRevision(): number { return revision; }
export function isMemoryEnabled(): boolean { return enabled; }

function updateMemoryEnabled(value: unknown): void {
  revision += 1;
  const next = parseConfigBoolean(value ?? false);
  if (next === enabled) return;
  enabled = next;
  listeners.forEach((listener) => listener());
}

// Publish successful saves across tabs; server snapshots remain authoritative on load.
export function setMemoryEnabled(value: unknown): void {
  updateMemoryEnabled(value);
  try {
    window.localStorage.setItem(MEMORY_SWITCH_EVENT, JSON.stringify({ enabled, at: Date.now() }));
  } catch {
    // Restricted storage must not prevent this tab from applying the saved switch.
  }
}

if (typeof window !== 'undefined') {
  window.addEventListener('storage', (event: StorageEvent) => {
    if (event.key !== MEMORY_SWITCH_EVENT || !event.newValue) return;
    try {
      const saved = JSON.parse(event.newValue);
      if (typeof saved?.enabled === 'boolean') {
        updateMemoryEnabled(saved.enabled);
        window.dispatchEvent(new window.Event(SETTINGS_CONFIG_CHANGED_EVENT));
      }
    } catch {
      // Ignore malformed notifications; the next config.get restores server state.
    }
  });
}

export function applyMemorySnapshot(value: unknown, expectedRevision: number): boolean {
  if (expectedRevision !== revision) return false;
  updateMemoryEnabled(value);
  return true;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function useMemoryEnabled(): boolean {
  return useSyncExternalStore(subscribe, isMemoryEnabled, isMemoryEnabled);
}
