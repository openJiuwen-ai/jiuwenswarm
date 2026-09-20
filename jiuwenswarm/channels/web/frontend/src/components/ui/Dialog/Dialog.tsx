import { useEffect, useRef, type ReactNode } from 'react';
import './Dialog.css';

export function Dialog({
  open,
  titleId,
  className,
  closeDisabled = false,
  onCancel,
  onBackdropClick,
  children,
}: {
  open: boolean;
  titleId: string;
  className?: string;
  closeDisabled?: boolean;
  onCancel: () => void;
  onBackdropClick?: () => void;
  children: ReactNode;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const pressedOnBackdrop = useRef(false);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);
  return (
    <dialog
      ref={dialogRef}
      className={`ui-dialog${className ? ` ${className}` : ''}`}
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault();
        if (!closeDisabled) onCancel();
      }}
      onPointerDown={(event) => {
        pressedOnBackdrop.current = event.target === event.currentTarget;
      }}
      onClick={(event) => {
        const onBackdrop = pressedOnBackdrop.current && event.target === event.currentTarget;
        pressedOnBackdrop.current = false;
        if (onBackdrop && !closeDisabled) onBackdropClick?.();
      }}
    >
      {children}
    </dialog>
  );
}
