const DEFAULT_TIMEOUT_SECONDS = 15;
const MAX_TIMEOUT_SECONDS = 2_147_483;
let invalidValueReported = false;

/** Read deploy-time configuration without rebuilding the frontend bundle. */
export function getDefaultRequestTimeoutMs(): number {
  const raw = typeof window === 'undefined' ? '' : String(window.__JIUWEN_WEB_REQUEST_TIMEOUT_SECONDS__ ?? '').trim();
  if (!raw || raw.startsWith('__')) {
    return DEFAULT_TIMEOUT_SECONDS * 1000;
  }
  const seconds = Number(raw);
  if (/^[0-9]{1,7}$/.test(raw) && seconds > 0 && seconds <= MAX_TIMEOUT_SECONDS) {
    return seconds * 1000;
  }
  if (!invalidValueReported) {
    console.warn('[UserWeb] Invalid WEB_REQUEST_TIMEOUT_SECONDS; using the default of 15 seconds.');
    invalidValueReported = true;
  }
  return DEFAULT_TIMEOUT_SECONDS * 1000;
}
