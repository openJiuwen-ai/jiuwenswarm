export const DUPLEX_SETTINGS_CHANGED_EVENT = 'jiuwen:duplex-settings-changed';

export function publishDuplexSettings(values: Record<string, unknown>, configuredSecretLengths: Record<string, number>): void {
  window.dispatchEvent(new CustomEvent(DUPLEX_SETTINGS_CHANGED_EVENT, {
    detail: { values, configured_secret_lengths: configuredSecretLengths },
  }));
}
