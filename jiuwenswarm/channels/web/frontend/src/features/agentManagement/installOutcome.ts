/**
 * Outcome of an expert install flow. `failed` is a definite rejection by the
 * backend (or a local failure that never let the request start); `uncertain`
 * means the request died mid-flight (timeout, dropped connection), so the
 * backend may still complete the install and callers must never roll back a
 * just-imported package on it.
 */
export type InstallOutcome = 'installed' | 'queued' | 'failed' | 'uncertain';

/**
 * Codes the web client raises itself while a request dies after being sent
 * but before the backend could answer (services/webClient.ts). Those are the
 * only genuinely unknown outcomes: the backend may still complete the
 * install, so a just-imported package must be kept. Codes like WS_NOT_READY
 * (request never sent) are deliberately excluded — the backend never started
 * the install, so rollback is safe. Every other error is a definite failure
 * — either the backend answered (any code counts as a verdict, including no
 * code at all: webClient leaves `code` undefined only on backend rejection
 * responses — resolvePending / resolveRuntimeAcceptedPending — never on
 * transport deaths, which always carry one of the codes above) or the
 * failure happened locally before the request could start. New web-client
 * transport codes must be added here.
 */
const INSTALL_TRANSPORT_ERROR_CODES: ReadonlySet<string> = new Set([
  'REQUEST_TIMEOUT',
  'WS_DISCONNECTED',
  'WS_CLOSED',
  'WS_ERROR',
  'REQUEST_ABORTED',
]);

export function isUncertainInstallError(error: unknown): boolean {
  if (!error || typeof error !== 'object' || !('code' in error)) return false;
  const code = String((error as { code?: unknown }).code || '').trim();
  return INSTALL_TRANSPORT_ERROR_CODES.has(code);
}
