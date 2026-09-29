export type ReplyLanguage = 'match' | 'zh-CN' | 'en';

export function normalizeReplyLanguage(value?: string | null): ReplyLanguage {
  const normalized = String(value || '').trim();
  if (normalized === 'zh-CN' || normalized === 'en' || normalized === 'match') return normalized;
  return 'match';
}

export function speakLanguageInstruction(replyLanguage: ReplyLanguage = 'match'): string {
  const preserve =
    'Preserve user-provided data, task IDs and file paths exactly.';
  if (replyLanguage === 'zh-CN') {
    return `Speak to the user in Simplified Chinese. ${preserve}`;
  }
  if (replyLanguage === 'en') {
    return `Speak to the user in English. ${preserve}`;
  }
  return (
    'Speak to the user in the same language as their latest utterance (speech transcript or typed text). ' +
    'If mixed, follow the latest user turn — not screen OCR language, not older assistant turns. ' +
    preserve
  );
}

export function announceLanguageInstruction(replyLanguage: ReplyLanguage = 'match'): string {
  if (replyLanguage === 'zh-CN') {
    return 'Respond naturally in one or two sentences of Simplified Chinese.';
  }
  if (replyLanguage === 'en') {
    return 'Respond naturally in one or two sentences of English.';
  }
  return (
    'Respond naturally in one or two sentences in the same language as the original user question ' +
    '(or the latest user utterance if the original language is unclear).'
  );
}

