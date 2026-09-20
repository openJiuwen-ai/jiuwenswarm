export type ImeKeyboardEvent = Pick<KeyboardEvent, 'isComposing' | 'key' | 'keyCode'>;

/**
 * Detect a key event handled by an input method editor.
 *
 * Safari/WKWebView can dispatch compositionend before the Enter keydown that
 * confirms an IME candidate. In that sequence isComposing is already false,
 * while the legacy keyCode remains 229 (the IME processing key). Some engines
 * expose the same state as key="Process" instead.
 */
export function isImeCompositionKey(event: ImeKeyboardEvent, compositionActive: boolean): boolean {
  return compositionActive || event.isComposing || event.key === 'Process' || event.keyCode === 229;
}
