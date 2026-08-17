import { useCallback, useEffect, useState } from 'react';

export type FontSizeKey = 'small' | 'medium' | 'large';

const STORAGE_KEY = 'jiuwenswarm.font-scale';

const SCALE_BY_KEY: Record<FontSizeKey, number> = {
  small: 0.9,
  medium: 1,
  large: 1.15,
};

function isValidKey(key: unknown): key is FontSizeKey {
  return key === 'small' || key === 'medium' || key === 'large';
}

function readStoredKey(): FontSizeKey {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (isValidKey(raw)) return raw;
  } catch {
    /* localStorage may be unavailable (private mode / sandboxed webview) */
  }
  return 'medium';
}

function applyScale(key: FontSizeKey): void {
  const el = document.documentElement;
  // Clear any leftover zoom from the earlier (reverted) zoom-based approach,
  // which would double-scale alongside --font-scale.
  el.style.removeProperty('zoom');
  // Write --font-scale on <html>. foundation.css multiplies every
  // --font-size-* token by it. Only elements using those tokens scale;
  // hardcoded font-size: Npx values are unaffected. Safer than zoom, which
  // overflowed the 100vh shell and hid the settings button.
  el.style.setProperty('--font-scale', String(SCALE_BY_KEY[key]));
}

/**
 * User-facing font-size preference (small / medium / large). Applies via the
 * --font-scale CSS variable, which multiplies every --font-size-* token.
 * Persists to localStorage so the choice survives reloads.
 */
export function useFontSize() {
  const [fontSize, setFontSize] = useState<FontSizeKey>(readStoredKey);

  // Apply on mount and whenever the key changes.
  useEffect(() => {
    applyScale(fontSize);
  }, [fontSize]);

  // Keep multiple components / tabs in sync via the storage event.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY && isValidKey(e.newValue)) {
        setFontSize(e.newValue);
      }
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  const changeFontSize = useCallback((key: FontSizeKey) => {
    setFontSize(key);
    try {
      localStorage.setItem(STORAGE_KEY, key);
    } catch {
      /* ignore write failures */
    }
  }, []);

  return { fontSize, changeFontSize };
}