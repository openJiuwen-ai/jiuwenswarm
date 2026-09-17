/**
 * TTS 相关工具函数
 */

import { webRequest } from '../services/webClient';

interface TtsResponse {
  success: boolean;
  audio_base64?: string;
  audio_mime?: string;
  error?: string;
}

const TTS_STOP_EVENT = 'jiuwen-tts-stop';
const CODE_BLOCK_RE = /```[\s\S]*?```/g;
const INLINE_CODE_RE = /`[^`]+`/g;
const MEDIA_BRACE_RE = /MEDIA:\{[^}]*\}/gi;
const MEDIA_SIMPLE_RE = /MEDIA:\S+/gi;
const URL_RE = /https?:\/\/\S+/g;
const WWW_RE = /www\.\S+/g;
const WIN_PATH_RE = /[A-Za-z]:\\[^\s]+/g;
const UNIX_PATH_RE = /(?:~|\/)(?:[^\s/]+\/)+[^\s/]*/g;
const QUOTE_BRACE_RE = /['"{}]/g;
const MULTI_NEWLINE_RE = /\n+/g;
const MULTI_PUNCT_RE = /。{2,}/g;
const MULTI_SPACE_RE = /\s{2,}/g;
const TRIM_EDGE_RE = /^[\s。:：]+|[\s。:：]+$/g;

export function sanitizeTtsText(
  input: string,
  maxLength = 500
): string {
  if (!input) {
    return '';
  }

  const sanitized = input
    .replace(CODE_BLOCK_RE, '代码块已省略')
    .replace(INLINE_CODE_RE, '')
    .replace(MEDIA_BRACE_RE, '')
    .replace(MEDIA_SIMPLE_RE, '')
    .replace(URL_RE, '')
    .replace(WWW_RE, '')
    .replace(WIN_PATH_RE, '')
    .replace(UNIX_PATH_RE, '')
    .replace(QUOTE_BRACE_RE, '')
    .replace(MULTI_NEWLINE_RE, '。')
    .replace(MULTI_PUNCT_RE, '。')
    .replace(MULTI_SPACE_RE, ' ')
    .replace(TRIM_EDGE_RE, '')
    .slice(0, maxLength)
    .trim();

  return sanitized;
}

// 全局音频实例，用于打断控制
let globalAudio: HTMLAudioElement | null = null;

export function stopGlobalAudio(): void {
  if (globalAudio) {
    globalAudio.pause();
    globalAudio.currentTime = 0;
    globalAudio = null;
  }
}

export function stopAllTts(): void {
  stopGlobalAudio();

  if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
    window.speechSynthesis.cancel();
  }

  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(TTS_STOP_EVENT));
  }
}

export function onTtsStop(handler: () => void): () => void {
  if (typeof window === 'undefined') {
    return () => {};
  }

  window.addEventListener(TTS_STOP_EVENT, handler);
  return () => window.removeEventListener(TTS_STOP_EVENT, handler);
}

export async function fetchTtsAudio(
  text: string,
  sessionId?: string,
  signal?: AbortSignal
): Promise<TtsResponse | null> {
  const trimmed = text.trim();
  if (!trimmed) {
    return null;
  }

  try {
    const params: Record<string, unknown> = { text: trimmed };
    if (sessionId) {
      params.session_id = sessionId;
    }
    const response = await webRequest<TtsResponse>('tts.synthesize', params, {
      signal,
    });
    return response;
  } catch (error) {
    console.warn('TTS 请求失败:', error);
    return null;
  }
}

export async function playAudioBase64(
  audioBase64: string,
  mimeType = 'audio/mpeg'
): Promise<boolean> {
  if (!audioBase64) {
    return false;
  }

  // 先停止正在播放的音频
  stopGlobalAudio();

  try {
    const audio = new Audio(`data:${mimeType};base64,${audioBase64}`);
    globalAudio = audio;
    audio.onended = () => {
      if (globalAudio === audio) {
        globalAudio = null;
      }
    };
    await audio.play();
    return true;
  } catch (error) {
    console.warn('播放音频失败:', error);
    return false;
  }
}
