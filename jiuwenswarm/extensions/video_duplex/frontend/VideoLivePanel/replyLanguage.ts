export type ReplyLanguage = 'match' | 'zh-CN' | 'en';
export function normalizeReplyLanguage(value?: string | null): ReplyLanguage { const v=String(value||'').trim(); return v==='en'||v==='zh-CN'||v==='match' ? v : 'match'; }
export function speakLanguageInstruction(language: ReplyLanguage='match'): string {
  const preserve='Preserve user-provided data, task IDs and file paths exactly.';
  if(language==='en') return `Speak to the user in English. ${preserve}`;
  if(language==='zh-CN') return `Speak to the user in Simplified Chinese. ${preserve}`;
  return `Speak to the user in the same language as their latest utterance (speech transcript or typed text). If mixed, follow the latest user turn — not screen OCR language, not older assistant turns. ${preserve}`;
}
export function announceLanguageInstruction(language: ReplyLanguage='match'): string { if(language==='en') return 'Respond naturally in one or two sentences of English.'; if(language==='zh-CN') return 'Respond naturally in one or two sentences of Simplified Chinese.'; return 'Respond naturally in one or two sentences in the same language as the original user question (or the latest user utterance if the original language is unclear).'; }
