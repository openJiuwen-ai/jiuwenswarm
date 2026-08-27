import { useEffect, useRef, type ReactNode } from 'react';
import './Dialog.css';

export function Dialog({
  open,
  titleId,
  className,
  closeDisabled = false,
  onCancel,
  children,
}: {
  open: boolean;
  titleId: string;
  className?: string;
  closeDisabled?: boolean;
  onCancel: () => void;
  children: ReactNode;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
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
    >
      {children}
    </dialog>
  );
}
