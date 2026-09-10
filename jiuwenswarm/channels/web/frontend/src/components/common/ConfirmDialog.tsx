/**
 * 通用确认弹窗（替代 window.confirm），统一可访问性与交互。
 */
import { useTranslation } from "react-i18next";
import { X } from "lucide-react";

interface ConfirmDialogProps {
  title: string;
  message: string;
  confirmLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
  loading?: boolean;
}

export function ConfirmDialog({
  title,
  message,
  confirmLabel,
  onConfirm,
  onCancel,
  loading = false,
}: ConfirmDialogProps) {
  const { t } = useTranslation();
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onCancel}>
      <div
        className="relative w-[420px] rounded-lg bg-card p-6 shadow-xl animate-rise"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-5 flex items-center justify-between">
          <h3 className="text-base font-semibold text-text-strong">{title}</h3>
          <button onClick={onCancel} className="text-text-muted hover:text-text" aria-label={t("common.close")}>
            <X size={20} />
          </button>
        </div>
        <p className="mb-6 break-words text-sm text-text">{message}</p>
        <div className="flex justify-center gap-3">
          <button
            onClick={onConfirm}
            disabled={loading}
            className="rounded-full bg-accent px-6 py-1.5 text-sm font-medium text-accent-foreground hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-60"
          >
            {confirmLabel || t("common.confirm")}
          </button>
          <button
            onClick={onCancel}
            className="px-6 py-1.5 rounded-full border border-border text-sm font-medium text-text hover:bg-secondary/50"
          >
            {t("common.cancel")}
          </button>
        </div>
      </div>
    </div>
  );
}
