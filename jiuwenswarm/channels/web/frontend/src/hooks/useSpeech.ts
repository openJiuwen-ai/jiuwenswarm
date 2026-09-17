/**
 * 语音输入输出 Hook
 *
 * 使用 Web Speech API 实现语音识别（STT）和语音合成（TTS）
 */

import { useState, useCallback, useRef, useEffect } from 'react';
import i18n from '../i18n';

// ============================================================================
// 语音识别 (STT)
// ============================================================================

interface UseSpeechRecognitionOptions {
  language?: string;
  continuous?: boolean;
  interimResults?: boolean;
  /** 无声音后多少毫秒结束识别，默认 5000。需配合 continuous: true 使用。 */
  silenceTimeoutMs?: number;
  /** 返回 true 时，onend 后会自动重启识别。 */
  restartWhen?: () => boolean;
  onResult?: (transcript: string, isFinal: boolean) => void;
  onError?: (error: string) => void;
  onEnd?: () => void;
}

interface UseSpeechRecognitionReturn {
  isListening: boolean;
  transcript: string;
  interimTranscript: string;
  startListening: () => void;
  stopListening: () => void;
  isSupported: boolean;
}

// Web Speech API 类型（部分浏览器/TS 未内置）
interface SpeechRecognitionEventMap {
  resultIndex: number;
  results: SpeechRecognitionResultList;
}
interface SpeechRecognitionInstance extends EventTarget {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start(): void;
  stop(): void;
  onstart: (() => void) | null;
  onend: (() => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onresult: ((event: SpeechRecognitionEventMap) => void) | null;
}
interface SpeechRecognitionConstructor {
  new (): SpeechRecognitionInstance;
}

declare global {
  interface Window {
    SpeechRecognition?: SpeechRecognitionConstructor;
    webkitSpeechRecognition?: SpeechRecognitionConstructor;
  }
}

// 供本文件内 ref 等使用
type SpeechRecognition = SpeechRecognitionInstance;

export function useSpeechRecognition(
  options: UseSpeechRecognitionOptions = {}
): UseSpeechRecognitionReturn {
  const {
    language = 'cmn-Hans-CN', // 普通话简体中文（比 zh-CN 更准确）
    continuous = false, // 默认检测到停止说话后自动结束
    interimResults = true,
    silenceTimeoutMs = 5000, // 无声音后 5s 结束（需配合 continuous: true）
    restartWhen,
    onResult,
    onError,
    onEnd,
  } = options;

  const [isListening, setIsListening] = useState(false);
  const [transcript, setTranscript] = useState('');
  const [interimTranscript, setInterimTranscript] = useState('');
  const recognitionRef = useRef<SpeechRecognition | null>(null);
  const silenceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const manualStopRef = useRef(false);
  const autoStopRef = useRef(false);
  const useContinuousRef = useRef(false);

  // 检查浏览器支持
  const isSupported =
    typeof window !== 'undefined' &&
    ('SpeechRecognition' in window || 'webkitSpeechRecognition' in window);

  const clearSilenceTimer = useCallback(() => {
    if (silenceTimerRef.current) {
      clearTimeout(silenceTimerRef.current);
      silenceTimerRef.current = null;
    }
  }, []);

  const scheduleSilenceStop = useCallback(() => {
    if (silenceTimeoutMs <= 0) {
      return;
    }
    clearSilenceTimer();
    silenceTimerRef.current = setTimeout(() => {
      silenceTimerRef.current = null;
      autoStopRef.current = true;
      recognitionRef.current?.stop();
    }, silenceTimeoutMs);
  }, [clearSilenceTimer, silenceTimeoutMs]);

  const startListening = useCallback(() => {
    if (!isSupported) {
      onError?.(i18n.t('speech.recognitionUnsupported'));
      return;
    }

    clearSilenceTimer();
    manualStopRef.current = false;
    autoStopRef.current = false;

    // 创建识别实例
    const SpeechRecognitionCtor =
      window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognitionCtor) {
      onError?.(i18n.t('speech.recognitionUnsupported'));
      return;
    }
    const recognition = new SpeechRecognitionCtor();

    // 使用自定义静默超时时，用 continuous=true 避免浏览器约 2s 就结束
    const useContinuous = continuous || silenceTimeoutMs > 0;
    useContinuousRef.current = useContinuous;
    recognition.lang = language;
    recognition.continuous = useContinuous;
    recognition.interimResults = interimResults;

    recognition.onstart = () => {
      setIsListening(true);
      setTranscript('');
      setInterimTranscript('');
      scheduleSilenceStop();
    };

    recognition.onresult = (event) => {
      let finalTranscript = '';
      let interim = '';

      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        if (result.isFinal) {
          finalTranscript += result[0].transcript;
        } else {
          interim += result[0].transcript;
        }
      }

      if (finalTranscript) {
        setTranscript((prev) => prev + finalTranscript);
        onResult?.(finalTranscript, true);
        scheduleSilenceStop();
      }

      setInterimTranscript(interim);
      if (interim) {
        onResult?.(interim, false);
        scheduleSilenceStop();
      }
    };

    recognition.onerror = (event) => {
      console.error('Speech recognition error:', event.error);
      clearSilenceTimer();
      setIsListening(false);
      
      const errorMessages: Record<string, string> = {
        'no-speech': i18n.t('speech.errors.noSpeech'),
        'audio-capture': i18n.t('speech.errors.noMic'),
        'not-allowed': i18n.t('speech.errors.notAllowed'),
        'network': i18n.t('speech.errors.network'),
      };
      
      onError?.(errorMessages[event.error] || i18n.t('speech.errors.recognitionGeneric', { error: event.error }));
    };

    recognition.onend = () => {
      clearSilenceTimer();
      if (manualStopRef.current) {
        manualStopRef.current = false;
        setIsListening(false);
        onEnd?.();
        return;
      }
      if (autoStopRef.current) {
        autoStopRef.current = false;
        setIsListening(false);
        onEnd?.();
        return;
      }
      if (useContinuousRef.current && restartWhen?.()) {
        try {
          recognitionRef.current?.start();
          return;
        } catch (error) {
          console.warn('Speech recognition restart failed:', error);
        }
      }
      setIsListening(false);
      onEnd?.();
    };

    recognitionRef.current = recognition;
    recognition.start();
  }, [isSupported, language, continuous, interimResults, silenceTimeoutMs, restartWhen, onResult, onError, onEnd, clearSilenceTimer]);

  const stopListening = useCallback(() => {
    clearSilenceTimer();
    if (recognitionRef.current) {
      manualStopRef.current = true;
      recognitionRef.current.stop();
      recognitionRef.current = null;
    }
    setIsListening(false);
  }, [clearSilenceTimer]);

  // 组件卸载时清理
  useEffect(() => {
    return () => {
      clearSilenceTimer();
      if (recognitionRef.current) {
        recognitionRef.current.stop();
      }
    };
  }, [clearSilenceTimer]);

  return {
    isListening,
    transcript,
    interimTranscript,
    startListening,
    stopListening,
    isSupported,
  };
}

// ============================================================================
// 语音合成 (TTS)
// ============================================================================

interface UseSpeechSynthesisOptions {
  language?: string;
  rate?: number;
  pitch?: number;
  volume?: number;
  onStart?: () => void;
  onEnd?: () => void;
  onError?: (error: string) => void;
}

interface UseSpeechSynthesisReturn {
  isSpeaking: boolean;
  speak: (text: string) => void;
  stop: () => void;
  pause: () => void;
  resume: () => void;
  isSupported: boolean;
  voices: SpeechSynthesisVoice[];
}

export function useSpeechSynthesis(
  options: UseSpeechSynthesisOptions = {}
): UseSpeechSynthesisReturn {
  const {
    language = 'zh-CN',
    rate = 1,
    pitch = 1,
    volume = 1,
    onStart,
    onEnd,
    onError,
  } = options;

  const [isSpeaking, setIsSpeaking] = useState(false);
  const [voices, setVoices] = useState<SpeechSynthesisVoice[]>([]);
  const utteranceRef = useRef<SpeechSynthesisUtterance | null>(null);

  // 检查浏览器支持
  const isSupported =
    typeof window !== 'undefined' && 'speechSynthesis' in window;

  // 加载可用语音
  useEffect(() => {
    if (!isSupported) return;

    const loadVoices = () => {
      const availableVoices = window.speechSynthesis.getVoices();
      setVoices(availableVoices);
    };

    loadVoices();
    window.speechSynthesis.onvoiceschanged = loadVoices;

    return () => {
      window.speechSynthesis.onvoiceschanged = null;
    };
  }, [isSupported]);

  const speak = useCallback(
    (text: string) => {
      if (!isSupported) {
        onError?.(i18n.t('speech.synthesisUnsupported'));
        return;
      }

      // 停止当前播放
      window.speechSynthesis.cancel();

      const utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = language;
      utterance.rate = rate;
      utterance.pitch = pitch;
      utterance.volume = volume;

      // 选择合适的中文语音
      const chineseVoice = voices.find(
        (v) => v.lang.includes('zh') || v.lang.includes('CN')
      );
      if (chineseVoice) {
        utterance.voice = chineseVoice;
      }

      utterance.onstart = () => {
        setIsSpeaking(true);
        onStart?.();
      };

      utterance.onend = () => {
        setIsSpeaking(false);
        onEnd?.();
      };

      utterance.onerror = (event) => {
        console.error('Speech synthesis error:', event);
        setIsSpeaking(false);
        onError?.(i18n.t('speech.errors.synthesisGeneric', { error: event.error }));
      };

      utteranceRef.current = utterance;
      window.speechSynthesis.speak(utterance);
    },
    [isSupported, language, rate, pitch, volume, voices, onStart, onEnd, onError]
  );

  const stop = useCallback(() => {
    if (isSupported) {
      window.speechSynthesis.cancel();
      setIsSpeaking(false);
    }
  }, [isSupported]);

  const pause = useCallback(() => {
    if (isSupported) {
      window.speechSynthesis.pause();
    }
  }, [isSupported]);

  const resume = useCallback(() => {
    if (isSupported) {
      window.speechSynthesis.resume();
    }
  }, [isSupported]);

  // 组件卸载时清理
  useEffect(() => {
    return () => {
      if (isSupported) {
        window.speechSynthesis.cancel();
      }
    };
  }, [isSupported]);

  return {
    isSpeaking,
    speak,
    stop,
    pause,
    resume,
    isSupported,
    voices,
  };
}
