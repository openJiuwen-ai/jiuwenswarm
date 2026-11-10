export type VoiceInputRoutingMode = 'leader' | 'none';

const storageKey = 'team-meeting-input-routing';
let fallback: VoiceInputRoutingMode = 'leader';

export function getVoiceInputRoutingMode(): VoiceInputRoutingMode {
  try {
    return window.localStorage.getItem(storageKey) === 'none' ? 'none' : 'leader';
  } catch {
    return fallback;
  }
}

export function setVoiceInputRoutingMode(mode: VoiceInputRoutingMode): void {
  fallback = mode;
  try {
    window.localStorage.setItem(storageKey, mode);
  } catch {
    // Keep the choice for this page when browser storage is unavailable.
  }
}
