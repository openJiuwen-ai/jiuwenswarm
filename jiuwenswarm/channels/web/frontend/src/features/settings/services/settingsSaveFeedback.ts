import { createContext } from 'react';
import type { TFunction } from 'i18next';

/** Registers settings that handle save errors locally, outside the page header. */
export const InlineSaveFeedbackContext = createContext<(operation: string) => () => void>(() => () => undefined);

export function formatSettingsSaveError(error: string | null, t: TFunction): string {
  if (error === 'Cannot enable KV cache affinity: No configured model declares KV cache affinity support') {
    return t('settingsPanel.affinity.noSupportingModel');
  }
  const missingJevKey =
    /^Jev API key is not configured(?: \([A-Za-z_][A-Za-z0-9_]*\))?$/.test(error ?? '') ||
    error === 'Jev API key is required in Models > Jev settings';
  return missingJevKey ? t('settingsPanel.browser.jevKeyMissing') : error || t('settingsPanel.feedback.saveFailed');
}
