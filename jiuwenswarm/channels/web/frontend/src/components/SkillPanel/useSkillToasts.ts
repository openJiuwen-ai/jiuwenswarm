/**
 * SkillPanel 全局 toast 消息 hook
 *
 * 从 index.tsx 抽取，逻辑保持不变。
 */
import { useCallback, useEffect, useRef, useState } from 'react';

export type SkillToastType = 'success' | 'error' | 'loading';

export type SkillToastShower = (type: SkillToastType, text: string) => void;

export function useSkillToasts() {
  const [message, setMessage] = useState<string | null>(null);
  const [messageType, setMessageType] = useState<SkillToastType | null>(null);
  const messageTimerRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (messageTimerRef.current !== null) {
        window.clearTimeout(messageTimerRef.current);
      }
    };
  }, []);

  const showMessage = useCallback((type: 'success' | 'error' | 'loading', text: string) => {
    if (messageTimerRef.current !== null) {
      window.clearTimeout(messageTimerRef.current);
      messageTimerRef.current = null;
    }
    const displayText = type === 'success' ? `√ ${text}` : text;
    setMessage(displayText);
    setMessageType(type);
    // loading 持续到下一次消息；错误信息显示时间更长（8秒）
    if (type === 'loading') {
      return;
    }
    const duration = type === 'error' ? 8000 : 3000;
    messageTimerRef.current = window.setTimeout(() => {
      setMessage(null);
      setMessageType(null);
      messageTimerRef.current = null;
    }, duration);
  }, []);

  const cleanMessage = message?.replace('√', '') || '';

  return { message, messageType, setMessage, setMessageType, showMessage, cleanMessage };
}
