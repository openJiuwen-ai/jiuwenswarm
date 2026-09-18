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
  onEscape?: () => void;
  escapeDisabled?: boolean;
  open?: boolean;
};

export function useDialogFocusTrap({
  dialogRef,
  restoreFocusRef,
  onEscape,
  escapeDisabled = false,
  open = true,
}: DialogFocusTrapOptions): void {
  const onEscapeRef = useRef(onEscape);
  const escapeDisabledRef = useRef(escapeDisabled);
  onEscapeRef.current = onEscape;
  escapeDisabledRef.current = escapeDisabled;

  useEffect(() => {
    if (!open) return undefined;
    const dialog = dialogRef.current;
    if (!dialog) return undefined;

    const activeElement = document.activeElement;
    const restoreTarget =
      restoreFocusRef?.current ??
      (activeElement instanceof HTMLElement && !dialog.contains(activeElement) ? activeElement : null);
    const getFocusableElements = () => Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
    const firstFocusable = getFocusableElements()[0];
    (firstFocusable || dialog).focus({ preventScroll: true });

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
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      if (restoreTarget?.isConnected) restoreTarget.focus({ preventScroll: true });
    };
  }, [dialogRef, open, restoreFocusRef]);
}
