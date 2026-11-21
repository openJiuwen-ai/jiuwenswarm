import { useEffect } from 'react';
import { AlertCircle, CheckCircle2, X } from 'lucide-react';

interface ToastProps {
  message: string;
  onClose: () => void;
  /** success（默认）/ error —— SwarmFlow 控制失败时用 error。 */
  variant?: 'success' | 'error';
}

export function Toast({ message, onClose, variant = 'success' }: ToastProps) {
  const isError = variant === 'error';

  useEffect(() => {
    const timer = setTimeout(onClose, isError ? 5200 : 2600);
    return () => clearTimeout(timer);
  }, [onClose, isError]);

  const palette = isError
    ? 'bg-danger-subtle text-danger hover:bg-danger-subtle'
    : 'bg-accent/10 text-accent hover:bg-accent/15';

  return (
    <div className="fixed right-6 top-6 z-[60]">
      <div
        className={`flex max-w-md items-start gap-2 rounded-lg ${palette} px-3 py-2 text-[13px] shadow-md`}
        data-testid="connector-market-toast"
        data-variant={variant}
      >
        {isError ? (
          <AlertCircle size={16} className="mt-0.5 shrink-0" />
        ) : (
          <CheckCircle2 size={16} className="mt-0.5 shrink-0" />
        )}
        <span className="min-w-0 flex-1 break-words max-h-60 overflow-y-auto">{message}</span>
        <button
          type="button"
          onClick={onClose}
          aria-label="close"
          className="ml-1 shrink-0 rounded p-0.5 opacity-70 hover:opacity-100"
          data-testid="connector-market-toast-close"
        >
          <X size={14} />
        </button>
      </div>
    </div>
  );
}
