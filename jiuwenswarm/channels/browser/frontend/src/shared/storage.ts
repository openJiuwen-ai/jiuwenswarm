/**
 * Typed wrappers around chrome.storage.local.
 *
 * Sessions are server-owned. The extension stores only:
 *   - ACTIVE_SESSION — which session is currently selected (pointer only)
 *   - PINNED_PAGES   — browser-specific research context (extracted page text)
 *   - SETTINGS       — host/port and behaviour preferences
 */

import { STORAGE_KEYS } from "./constants";
import {
  DEFAULT_SETTINGS,
  ExtensionSettings,
  PinnedPage,
  ChatEntry,
} from "./types";

// ---------------------------------------------------------------------------
// Active session pointer (server owns the full session list)
// ---------------------------------------------------------------------------

export async function loadActiveSessionId(): Promise<string | null> {
  const result = await chrome.storage.local.get(STORAGE_KEYS.ACTIVE_SESSION);
  return (result[STORAGE_KEYS.ACTIVE_SESSION] as string) ?? null;
}

export async function saveActiveSessionId(id: string | null): Promise<void> {
  await chrome.storage.local.set({ [STORAGE_KEYS.ACTIVE_SESSION]: id });
}

// ---------------------------------------------------------------------------
// Pinned pages
// ---------------------------------------------------------------------------

export async function loadPinnedPages(): Promise<PinnedPage[]> {
  const result = await chrome.storage.local.get(STORAGE_KEYS.PINNED_PAGES);
  return (result[STORAGE_KEYS.PINNED_PAGES] as PinnedPage[]) ?? [];
}

export async function savePinnedPages(pages: PinnedPage[]): Promise<void> {
  await chrome.storage.local.set({ [STORAGE_KEYS.PINNED_PAGES]: pages });
}

export async function addPinnedPage(page: PinnedPage): Promise<void> {
  const pages = await loadPinnedPages();
  pages.push(page);
  await savePinnedPages(pages);
}

export async function removePinnedPage(id: string): Promise<void> {
  const pages = await loadPinnedPages();
  await savePinnedPages(pages.filter((p) => p.id !== id));
}

export async function getPinnedPagesBySession(
  sessionId: string
): Promise<PinnedPage[]> {
  const pages = await loadPinnedPages();
  return pages.filter((p) => p.sessionId === sessionId);
}

/** Move a pinned page one position up (dir=-1) or down (dir=+1) within its session. */
export async function movePinnedPage(
  sessionId: string,
  id: string,
  dir: -1 | 1
): Promise<void> {
  const pages = await loadPinnedPages();
  const idx = pages.findIndex((p) => p.id === id);
  const target = idx + dir;
  if (idx < 0 || target < 0 || target >= pages.length) return;
  if (pages[target].sessionId !== sessionId) return; // never cross sessions
  [pages[idx], pages[target]] = [pages[target], pages[idx]];
  await savePinnedPages(pages);
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

export async function loadSettings(): Promise<ExtensionSettings> {
  const result = await chrome.storage.local.get(STORAGE_KEYS.SETTINGS);
  const stored = result[STORAGE_KEYS.SETTINGS] as Partial<ExtensionSettings> | undefined;
  return { ...DEFAULT_SETTINGS, ...(stored ?? {}) };
}

export async function saveSettings(
  settings: Partial<ExtensionSettings>
): Promise<void> {
  const current = await loadSettings();
  await chrome.storage.local.set({
    [STORAGE_KEYS.SETTINGS]: { ...current, ...settings },
  });
}

// ---------------------------------------------------------------------------
// First-run tour
// ---------------------------------------------------------------------------

export async function hasSeenTour(): Promise<boolean> {
  const result = await chrome.storage.local.get(STORAGE_KEYS.HAS_SEEN_TOUR);
  return (result[STORAGE_KEYS.HAS_SEEN_TOUR] as boolean) ?? false;
}

export async function markTourSeen(): Promise<void> {
  await chrome.storage.local.set({ [STORAGE_KEYS.HAS_SEEN_TOUR]: true });
}

// ---------------------------------------------------------------------------
// Last response cache (offline re-reading)
// ---------------------------------------------------------------------------

export async function saveLastResponse(text: string): Promise<void> {
  await chrome.storage.local.set({
    [STORAGE_KEYS.LAST_RESPONSE]: { text, savedAt: new Date().toISOString() },
  });
}

export async function loadLastResponse(): Promise<{ text: string; savedAt: string } | null> {
  const result = await chrome.storage.local.get(STORAGE_KEYS.LAST_RESPONSE);
  return (result[STORAGE_KEYS.LAST_RESPONSE] as { text: string; savedAt: string } | undefined) ?? null;
}

// ---------------------------------------------------------------------------
// Chat history (per session)
// ---------------------------------------------------------------------------

const MAX_HISTORY = 200;

export async function loadChatHistory(sessionId: string): Promise<ChatEntry[]> {
  const result = await chrome.storage.local.get(STORAGE_KEYS.CHAT_HISTORY);
  const map = (result[STORAGE_KEYS.CHAT_HISTORY] as Record<string, ChatEntry[]>) ?? {};
  return map[sessionId] ?? [];
}

export async function saveChatHistory(
  sessionId: string,
  entries: ChatEntry[]
): Promise<void> {
  const result = await chrome.storage.local.get(STORAGE_KEYS.CHAT_HISTORY);
  const map = (result[STORAGE_KEYS.CHAT_HISTORY] as Record<string, ChatEntry[]>) ?? {};
  map[sessionId] = entries.slice(-MAX_HISTORY);
  await chrome.storage.local.set({ [STORAGE_KEYS.CHAT_HISTORY]: map });
}

// ---------------------------------------------------------------------------
// Session display names (local rename override)
// ---------------------------------------------------------------------------

export async function loadSessionDisplayNames(): Promise<Record<string, string>> {
  const result = await chrome.storage.local.get(STORAGE_KEYS.SESSION_NAMES);
  return (result[STORAGE_KEYS.SESSION_NAMES] as Record<string, string>) ?? {};
}

/** Set a local display name for a session. An empty name clears the override. */
export async function saveSessionDisplayName(
  sessionId: string,
  name: string
): Promise<Record<string, string>> {
  const map = await loadSessionDisplayNames();
  if (name.trim()) {
    map[sessionId] = name.trim();
  } else {
    delete map[sessionId];
  }
  await chrome.storage.local.set({ [STORAGE_KEYS.SESSION_NAMES]: map });
  return map;
}
