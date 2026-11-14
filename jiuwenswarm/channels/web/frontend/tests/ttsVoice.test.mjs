import assert from 'node:assert/strict';
import test from 'node:test';
import {
  detectTtsLanguage,
  findPromptBefore,
  pickTtsVoice,
  ttsLanguageForLocale,
} from '../node_modules/.cache/tts-voice/ttsVoice.mjs';

const voice = (lang, name = lang, isDefault = false) => ({ lang, name, default: isDefault });

test('English replies are read in English', () => {
  assert.equal(detectTtsLanguage('Here is a summary of the three fruits you asked for.'), 'en-US');
  assert.equal(detectTtsLanguage("It's done. Let me know if you'd like changes."), 'en-US');
});

test('Chinese replies are read in Chinese', () => {
  assert.equal(detectTtsLanguage('这是您要的三种水果的总结。'), 'zh-CN');
});

test('Chinese text with embedded English terms stays Chinese', () => {
  assert.equal(detectTtsLanguage('使用 React 和 TypeScript 开发前端页面'), 'zh-CN');
});

test('a few Han characters do not switch an English reply to Chinese', () => {
  assert.equal(detectTtsLanguage('Hello 张三, your weekly report is ready to review.'), 'en-US');
  // sanitizeTtsText 会把代码块替换为中文占位符
  assert.equal(
    detectTtsLanguage('Run the script below to install the package and restart the service. 代码块已省略'),
    'en-US'
  );
});

test('text without letters falls back to the configured language', () => {
  assert.equal(detectTtsLanguage('123 456。'), 'zh-CN');
  assert.equal(detectTtsLanguage('', 'en-US'), 'en-US');
});

test('a numbers-only reply follows the language of the prompt', () => {
  const reply = '1, 2, 3, 4, 5, 6, 7, 8, 9, 10';
  assert.equal(detectTtsLanguage(reply, detectTtsLanguage('count from 1 to 10', 'zh-CN')), 'en-US');
  assert.equal(detectTtsLanguage(reply, detectTtsLanguage('从1数到10', 'en-US')), 'zh-CN');
  // 回复和输入都无法判定时才用兜底语言
  assert.equal(detectTtsLanguage(reply, detectTtsLanguage('', 'en-US')), 'en-US');
});

test('findPromptBefore returns the closest earlier user message', () => {
  const messages = [
    { id: 'u1', role: 'user', content: 'first question' },
    { id: 'a1', role: 'assistant', content: 'first answer' },
    { id: 'u2', role: 'user', content: 'count from 1 to 10' },
    { id: 't1', role: 'tool', content: '' },
    { id: 'a2', role: 'assistant', content: '1, 2, 3' },
  ];
  assert.equal(findPromptBefore(messages, 'a2'), 'count from 1 to 10');
  assert.equal(findPromptBefore(messages, 'a1'), 'first question');
  assert.equal(findPromptBefore(messages, 'u1'), '');
  assert.equal(findPromptBefore(messages, 'missing'), '');
});

test('ttsLanguageForLocale maps the UI language', () => {
  assert.equal(ttsLanguageForLocale('en'), 'en-US');
  assert.equal(ttsLanguageForLocale('en-GB'), 'en-US');
  assert.equal(ttsLanguageForLocale('zh'), 'zh-CN');
  assert.equal(ttsLanguageForLocale(undefined), 'zh-CN');
});

test('pickTtsVoice prefers an exact locale, then the same language, then the default voice', () => {
  const voices = [voice('zh-CN', 'Tingting'), voice('en-GB', 'Daniel'), voice('en-US', 'Samantha')];
  assert.equal(pickTtsVoice(voices, 'en-US')?.name, 'Samantha');
  assert.equal(pickTtsVoice(voices, 'zh-CN')?.name, 'Tingting');
  assert.equal(pickTtsVoice([voice('en-GB', 'Daniel')], 'en-US')?.name, 'Daniel');
  assert.equal(
    pickTtsVoice([voice('en-US', 'Alex'), voice('en-US', 'Samantha', true)], 'en-US')?.name,
    'Samantha'
  );
});

test('pickTtsVoice normalizes locale formats and Chinese variants', () => {
  assert.equal(pickTtsVoice([voice('en_US', 'Android EN')], 'en-US')?.name, 'Android EN');
  assert.equal(pickTtsVoice([voice('cmn-Hans-CN', 'Android ZH')], 'zh-CN')?.name, 'Android ZH');
});

test('pickTtsVoice returns undefined when no voice matches the language', () => {
  assert.equal(pickTtsVoice([voice('zh-CN')], 'en-US'), undefined);
  assert.equal(pickTtsVoice([], 'zh-CN'), undefined);
});
