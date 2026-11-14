import { useEffect, useRef, type RefObject } from 'react';

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'area[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[contenteditable="true"]',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

type DialogFocusTrapOptions = {
  dialogRef: RefObject<HTMLElement | null>;
  restoreFocusRef?: RefObject<HTMLElement | null>;
  /** 目标元素已脱离文档（如所在列表项被删除）时的替代落点。 */
  restoreFocusFallback?: () => HTMLElement | null;
  initialFocusRef?: RefObject<HTMLElement | null>;
  onEscape?: () => void;
  escapeDisabled?: boolean;
  open?: boolean;
};

export function useDialogFocusTrap({
  dialogRef,
  restoreFocusRef,
  restoreFocusFallback,
  initialFocusRef,
  onEscape,
  escapeDisabled = false,
  open = true,
}: DialogFocusTrapOptions): void {
  const onEscapeRef = useRef(onEscape);
  const escapeDisabledRef = useRef(escapeDisabled);
  const restoreFocusFallbackRef = useRef(restoreFocusFallback);
  onEscapeRef.current = onEscape;
  escapeDisabledRef.current = escapeDisabled;
  restoreFocusFallbackRef.current = restoreFocusFallback;

  useEffect(() => {
    if (!open) return undefined;
    const dialog = dialogRef.current;
    if (!dialog) return undefined;

    // activeElement 恒为 Element | null；不用 instanceof 判断，避免 jsdom 等环境缺少全局构造器
    const activeElement = document.activeElement;
    const restoreTarget =
      restoreFocusRef?.current ??
      (activeElement && activeElement !== document.body && !dialog.contains(activeElement)
        ? (activeElement as HTMLElement)
        : null);
    const getFocusableElements = () => Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
    const firstFocusable = getFocusableElements()[0];
    const preferredFocus = initialFocusRef?.current;
    (preferredFocus?.isConnected ? preferredFocus : firstFocusable || dialog).focus({ preventScroll: true });

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        if (!escapeDisabledRef.current) onEscapeRef.current?.();
        return;
      }
      if (event.key !== 'Tab') return;

      const focusableElements = getFocusableElements();
      if (focusableElements.length === 0) {
        event.preventDefault();
        dialog.focus({ preventScroll: true });
        return;
      }
      const first = focusableElements[0];
      const last = focusableElements[focusableElements.length - 1];
      const active = document.activeElement;
      // 焦点可能被 blur/禁用按钮等甩到弹窗外，或落在 tabIndex=-1 的容器自身上
      // （点击弹窗空白/文案时）。此时浏览器会按文档顺序继续找可聚焦元素，直接跑出弹窗。
      const activeInside = !!active && active !== dialog && dialog.contains(active);
      if (!activeInside) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
        return;
      }
      if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      if (!restoreTarget) return;
      if (restoreTarget.isConnected) {
        restoreTarget.focus({ preventScroll: true });
        return;
      }
      const fallback = restoreFocusFallbackRef.current?.();
      if (fallback?.isConnected) fallback.focus({ preventScroll: true });
    };
  }, [dialogRef, open, restoreFocusRef, initialFocusRef]);
}
