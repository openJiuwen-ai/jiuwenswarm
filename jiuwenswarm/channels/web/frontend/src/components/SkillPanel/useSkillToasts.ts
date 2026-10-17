/**
 * SkillPanel 全局 toast 消息 hook
 *
 * 从 index.tsx 抽取，逻辑保持不变。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from '../ui/Toast/toastStore';

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
    if (type === 'error') {
      setMessage(null);
      setMessageType(null);
      toast.open({ id: 'skill-panel-action-error', content: text, variant: 'error', duration: 8 });
      return;
    }
    const displayText = type === 'success' ? `√ ${text}` : text;
    setMessage(displayText);
    setMessageType(type);
    // loading 持续到下一次消息；success 3 秒自动消失（error 已改走全局 toast）
    if (type === 'loading') {
      return;
    }
    messageTimerRef.current = window.setTimeout(() => {
      setMessage(null);
      setMessageType(null);
      messageTimerRef.current = null;
    }, 3000);
  }, []);

  const cleanMessage = message?.replace('√', '') || '';

  return { message, messageType, setMessage, setMessageType, showMessage, cleanMessage };
}
