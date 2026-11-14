/**
 * TTS 朗读语言与语音选择
 *
 * 按文本内容选择朗读语言，避免英文回复被中文语音读出。
 */

export type TtsLanguage = 'zh-CN' | 'en-US';

const HAN_CHAR_RE = /\p{Script=Han}/gu;
const LATIN_WORD_RE = /[A-Za-z]+(?:['’][A-Za-z]+)*/g;
const CHINESE_PRIMARY_TAGS = new Set(['zh', 'cmn', 'yue']);

/**
 * 按「汉字数 ≥ 英文单词数」判定中文，否则判定英文；没有可读文字时返回 fallback。
 *
 * 以英文单词而非字母计数：中文里夹杂的术语（如「使用 React 开发」）仍按中文朗读，
 * 英文回复里零星的汉字（如人名）不会让整段切到中文语音。
 */
export function detectTtsLanguage(text: string, fallback: string = 'zh-CN'): string {
  const hanCount = text.match(HAN_CHAR_RE)?.length ?? 0;
  const latinWordCount = text.match(LATIN_WORD_RE)?.length ?? 0;
  if (hanCount === 0 && latinWordCount === 0) {
    return fallback;
  }
  return hanCount >= latinWordCount ? 'zh-CN' : 'en-US';
}

/** 找到某条回复之前最近的一条用户消息内容，作为朗读语言的兜底依据；找不到时返回空串。 */
export function findPromptBefore(
  messages: readonly { id: string; role: string; content: string }[],
  messageId: string
): string {
  const index = messages.findIndex((m) => m.id === messageId);
  for (let i = index - 1; i >= 0; i -= 1) {
    if (messages[i].role === 'user') {
      return messages[i].content;
    }
  }
  return '';
}

/** 界面语言（i18n，如 en / zh）对应的朗读语言，用作纯数字等无法判定文本的兜底。 */
export function ttsLanguageForLocale(locale: string | undefined): TtsLanguage {
  return locale?.toLowerCase().startsWith('en') ? 'en-US' : 'zh-CN';
}

function normalizeLang(lang: string): string {
  return lang.replace(/_/g, '-').toLowerCase();
}

function primaryTag(lang: string): string {
  const primary = normalizeLang(lang).split('-')[0];
  return CHINESE_PRIMARY_TAGS.has(primary) ? 'zh' : primary;
}

/**
 * 为目标语言挑选语音：优先完全匹配地区（en-US），其次同语种（en-GB），
 * 同档内优先浏览器默认语音；找不到时返回 undefined，交给浏览器按 utterance.lang 选择。
 */
export function pickTtsVoice<V extends Pick<SpeechSynthesisVoice, 'lang' | 'default'>>(
  voices: readonly V[],
  lang: string
): V | undefined {
  const target = normalizeLang(lang);
  const targetPrimary = primaryTag(lang);
  const preferDefault = (candidates: V[]) => candidates.find((v) => v.default) ?? candidates[0];

  const exact = voices.filter((v) => normalizeLang(v.lang) === target);
  if (exact.length > 0) {
    return preferDefault(exact);
  }
  const samePrimary = voices.filter((v) => primaryTag(v.lang) === targetPrimary);
  return samePrimary.length > 0 ? preferDefault(samePrimary) : undefined;
}
