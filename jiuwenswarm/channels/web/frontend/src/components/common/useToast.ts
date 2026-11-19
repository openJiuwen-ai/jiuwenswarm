import { useCallback, useEffect, useRef, useState } from 'react';

export type ToastKind = 'success' | 'error';

export type ToastState = {
  type: ToastKind;
  text: string;
};

const TOAST_TTL_MS = 3000;

/** 面板内联轻提示：success/error，约 3s 自动消失。 */
export function useToast() {
  const [toast, setToast] = useState<ToastState | null>(null);
  const timerRef = useRef<number | null>(null);

  const clearToast = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    setToast(null);
  }, []);

  const showToast = useCallback(
    (type: ToastKind, text: string) => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      setToast({ type, text });
      timerRef.current = window.setTimeout(() => {
        setToast(null);
        timerRef.current = null;
      }, TOAST_TTL_MS);
    },
    [],
  );

  useEffect(
    () => () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    },
    [],
  );

  return { toast, showToast, clearToast };
}
