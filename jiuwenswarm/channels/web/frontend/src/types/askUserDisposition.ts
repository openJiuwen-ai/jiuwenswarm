/**
 * Language-agnostic ask_user Skip/Cancel sentinels.
 * Must stay in sync with jiuwenswarm.agents.harness.common.rails.ask_user_disposition.
 */

export const ASK_USER_SKIPPED = '[ASK_USER_SKIPPED]';
export const ASK_USER_CANCELLED = '[ASK_USER_CANCELLED]';

/** Legacy zh/en fixed phrases (recognize only; new clients must not emit these). */
export const LEGACY_ASK_USER_SKIPPED = [
  '用户已跳过此题，未选择任何选项。',
  'Skipped. No option selected.',
] as const;

export const LEGACY_ASK_USER_CANCELLED = [
  '用户已取消本次问答，未作答。',
  'Cancelled. No answer provided.',
] as const;

export type AskUserDisposition = 'skipped' | 'cancelled';

export function isAskUserSkipOrCancelDisposition(
  disposition: string | undefined | null,
): disposition is AskUserDisposition {
  return disposition === 'skipped' || disposition === 'cancelled';
}

export function isAskUserSkipOrCancelText(text: string | undefined | null): boolean {
  const value = (text || '').trim();
  if (!value) return false;
  if (value === ASK_USER_SKIPPED || value === ASK_USER_CANCELLED) return true;
  return (
    (LEGACY_ASK_USER_SKIPPED as readonly string[]).includes(value)
    || (LEGACY_ASK_USER_CANCELLED as readonly string[]).includes(value)
  );
}

/** Map wire/history text to a display i18n key under interactionPrompt.* */
export function askUserDispositionDisplayKey(
  text: string | undefined | null,
): 'skippedAnswer' | 'cancelledAnswer' | null {
  const value = (text || '').trim();
  if (!value) return null;
  if (
    value === ASK_USER_SKIPPED
    || (LEGACY_ASK_USER_SKIPPED as readonly string[]).includes(value)
    || value.includes(ASK_USER_SKIPPED)
  ) {
    return 'skippedAnswer';
  }
  if (
    value === ASK_USER_CANCELLED
    || (LEGACY_ASK_USER_CANCELLED as readonly string[]).includes(value)
    || value.includes(ASK_USER_CANCELLED)
  ) {
    return 'cancelledAnswer';
  }
  for (const marker of LEGACY_ASK_USER_SKIPPED) {
    if (value.includes(marker)) return 'skippedAnswer';
  }
  for (const marker of LEGACY_ASK_USER_CANCELLED) {
    if (value.includes(marker)) return 'cancelledAnswer';
  }
  return null;
}

/** Remap a single answer / result line for UI display. */
export function remapAskUserDispositionLine(
  text: string,
  t: (key: string) => string,
): string {
  const key = askUserDispositionDisplayKey(text);
  if (!key) return text;
  const localized = t(`interactionPrompt.${key}`);
  // tool_result lines look like "question: sentinel" — replace the value part only.
  if (text.includes(ASK_USER_SKIPPED)) {
    return text.split(ASK_USER_SKIPPED).join(localized);
  }
  if (text.includes(ASK_USER_CANCELLED)) {
    return text.split(ASK_USER_CANCELLED).join(localized);
  }
  for (const marker of LEGACY_ASK_USER_SKIPPED) {
    if (text.includes(marker)) return text.split(marker).join(localized);
  }
  for (const marker of LEGACY_ASK_USER_CANCELLED) {
    if (text.includes(marker)) return text.split(marker).join(localized);
  }
  return localized;
}

export function remapAskUserDispositionResult(
  result: string,
  t: (key: string) => string,
): string {
  if (!result) return result;
  return result
    .split('\n')
    .map((line) => remapAskUserDispositionLine(line, t))
    .join('\n');
}
