const STOP_EVENT = 'jiuwen-tts-stop';

export function requestTtsStop(): void {
  if (typeof window !== 'undefined') window.dispatchEvent(new window.Event(STOP_EVENT));
}

export function onTtsStop(handler: () => void): () => void {
  if (typeof window === 'undefined') return () => {};
  window.addEventListener(STOP_EVENT, handler);
  return () => window.removeEventListener(STOP_EVENT, handler);
}
