import type { ReactNode } from "react";

/**
 * 字段名 + 问号提示（样式对齐 Cron 定时任务：圆圈 ? + title 悬停）。
 * 无 help 时仅渲染标签。
 */
export function ConfigFieldHintLabel({
  label,
  help,
  className,
  mono = false,
}: {
  label: ReactNode;
  help?: string;
  className?: string;
  mono?: boolean;
}) {
  if (!help) {
    return <div data-testid="config-panel-field-hint-label-1" className={`${mono ? "mono" : ""} ${className ?? ""}`.trim()}>{label}</div>;
  }

  return (
    <div data-testid="config-panel-field-hint-label-2" className={`inline-flex max-w-full items-center gap-1 ${mono ? "mono" : ""} ${className ?? ""}`.trim()}>
      <span>{label}</span>
      <span
        data-testid="config-panel-field-hint-label-2-help"
        className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-border text-[10px] font-normal text-text-muted cursor-help"
        title={help}
        aria-label={help}
      >
        ?
      </span>
    </div>
  );
}
