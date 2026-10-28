import { createContext } from 'react';
import type { TFunction } from 'i18next';

/** Registers mounted settings that display their save errors next to the control. */
export const InlineSaveFeedbackContext = createContext<(operation: string) => () => void>(() => () => undefined);

export function formatSettingsSaveError(error: string | null, t: TFunction): string {
  const missingJevKey =
    /^Jev API key is not configured(?: \([A-Za-z_][A-Za-z0-9_]*\))?$/.test(error ?? '') ||
    error === 'Jev API key is required in Models > Jev settings';
  return missingJevKey ? t('settingsPanel.browser.jevKeyMissing') : error || t('settingsPanel.feedback.saveFailed');
}
