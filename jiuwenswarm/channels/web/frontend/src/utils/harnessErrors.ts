/**
 * Harness package error → localized message mapping.
 *
 * The backend returns a structured `code` (CONFLICT / NOT_FOUND / BAD_REQUEST /
 * INTERNAL_ERROR) on harness package failures. Map it to an i18n string so the
 * UI never shows the locale-unaware backend `error` text directly.
 */

import i18n from 'i18next';
import type { WebError } from '../types/websocket';

const CODE_TO_KEY: Record<string, string> = {
  CONFLICT: 'harnessPackage.packageExists',
  NOT_FOUND: 'harnessPackage.packageNotFound',
  BAD_REQUEST: 'harnessPackage.invalidRequest',
  INTERNAL_ERROR: 'harnessPackage.internalError',
};

/**
 * Resolve a harness package error to a localized message.
 *
 * @param err The caught error (typically a WebError from webClient).
 * @param fallbackKey i18n key used when the error has no recognized code
 *   (e.g. 'harnessPackage.activateFailed').
 */
export function resolveHarnessError(err: unknown, fallbackKey: string): string {
  const code = (err as WebError | undefined)?.code;
  if (code && CODE_TO_KEY[code]) {
    return i18n.t(CODE_TO_KEY[code]);
  }
  return i18n.t(fallbackKey);
}
