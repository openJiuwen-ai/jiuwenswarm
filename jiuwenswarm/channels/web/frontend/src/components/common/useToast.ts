/**
 * 通用 toast 提示 hook：统一成功/失败结果反馈。
 */
import { useCallback, useEffect, useRef, useState } from "react";

export type ToastType = "success" | "error";

export interface ToastState {
  type: ToastType;
  text: string;
}

export function useToast(durationMs = 3000) {
  const [toast, setToast] = useState<ToastState | null>(null);
  const timerRef = useRef<number | null>(null);

  const showToast = useCallback(
    (type: ToastType, text: string) => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      setToast({ type, text });
      timerRef.current = window.setTimeout(() => {
        setToast(null);
        timerRef.current = null;
      }, durationMs);
    },
    [durationMs]
  );

  const clearToast = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    setToast(null);
  }, []);

  useEffect(
    () => () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    },
    []
  );

  return { toast, showToast, clearToast };
}
