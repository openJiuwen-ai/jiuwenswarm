import { useEffect, useSyncExternalStore } from 'react';
import { createPortal } from 'react-dom';
import { AlertTriangle, CircleAlert, CircleCheck, X } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { toast, toastStore, type ToastRecord, type ToastVariant } from './toastStore';
import './Toast.css';

/** 各变体对应的状态图标；default/info 无图标。自定义 icon 优先于变体图标。 */
const VARIANT_ICON: Partial<Record<ToastVariant, LucideIcon>> = {
  success: CircleCheck,
  warning: AlertTriangle,
  error: CircleAlert,
};

function ToastItem({ record }: { record: ToastRecord }) {
  const { key, content, actions, durationMs, variant, icon, wide, closing, testId, dataVariant, style, closable, updatedAt } = record;
  // 自动消失计时器依赖 updatedAt：同 id 原地更新时序号必然递增 → 计时器复位（对齐 antd update 语义）
  useEffect(() => {
    if (durationMs <= 0) return undefined;
    const timerId = window.setTimeout(() => toast.close(key), durationMs);
    return () => window.clearTimeout(timerId);
  }, [key, durationMs, updatedAt]);
  const StatusIcon = VARIANT_ICON[variant];
  const hasActions = actions.length > 0;
  return (
    <div
      className={`ui-toast${variant === 'default' ? '' : ` ui-toast--${variant}`}${hasActions ? ' ui-toast--with-actions' : ''}${wide ? ' ui-toast--wide' : ''}${closing ? ' ui-toast--closing' : ''}`}
      role="status"
      style={style}
      data-testid={testId ?? 'ui-toast'}
      data-variant={dataVariant ?? key}
    >
      <div className="ui-toast__body">
        {icon ? (
          <span className="ui-toast__status-icon" aria-hidden="true">{icon}</span>
        ) : StatusIcon ? (
          <StatusIcon className="ui-toast__status-icon" aria-hidden="true" size={14} />
        ) : null}
        <span className="ui-toast__content">{content}</span>
      </div>
      {hasActions ? (
        <span className="ui-toast__actions">
          {actions.map((action, index) => (
            <button
              key={index}
              type="button"
              className="ui-toast__action"
              onClick={() => {
                action.onClick?.();
                toast.close(key);
              }}
              data-testid="ui-toast-action"
              data-variant={index}
            >
              {action.label}
            </button>
          ))}
        </span>
      ) : null}
      {closable ? (
        <button
          type="button"
          className="ui-toast__close"
          aria-label="close"
          title="close"
          onClick={() => toast.close(key)}
          data-testid="ui-toast-close"
        >
          <X aria-hidden="true" size={16} />
        </button>
      ) : null}
    </div>
  );
}

/** 全局命令式 toast 的渲染出口：应用内挂载一次，toast.open() 的内容经此渲染到 body。按 position 分列渲染。 */
export function ToastStack() {
  const records = useSyncExternalStore(toastStore.subscribe, toastStore.getSnapshot, toastStore.getSnapshot);
  if (records.length === 0) return null;
  const centerRecords = records.filter((record) => record.position !== 'right');
  const rightRecords = records.filter((record) => record.position === 'right');
  return createPortal(
    <>
      {centerRecords.length > 0 && (
        <div className="ui-toast-stack" data-testid="ui-toast-stack">
          {centerRecords.map((record) => (
            <ToastItem key={record.key} record={record} />
          ))}
        </div>
      )}
      {rightRecords.length > 0 && (
        <div className="ui-toast-stack ui-toast-stack--right" data-testid="ui-toast-stack-right">
          {rightRecords.map((record) => (
            <ToastItem key={record.key} record={record} />
          ))}
        </div>
      )}
    </>,
    document.body,
  );
}
