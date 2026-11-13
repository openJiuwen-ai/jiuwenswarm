/**
 * Whether a key belongs to the input method, not to the pane. Safari and
 * Office's Mac web view end the composition before the Enter that picks a
 * candidate, so ``isComposing`` is already false there while the legacy
 * ``keyCode`` is still 229; some engines say ``key`` "Process" instead.
 */
export function isImeKey(event: KeyboardEvent): boolean {
  return event.isComposing || event.key === 'Process' || event.keyCode === 229;
}
