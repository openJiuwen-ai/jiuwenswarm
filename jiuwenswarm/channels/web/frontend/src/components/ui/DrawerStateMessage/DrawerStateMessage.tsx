import './DrawerStateMessage.css';

export type DrawerStateVariant = 'loading' | 'error' | 'empty';

export interface DrawerStateMessageProps {
  variant: DrawerStateVariant;
  message: string;
  /** variant='error' 时的重试按钮文案 */
  retryLabel?: string;
  onRetry?: () => void;
  /** 容器 testid */
  testId?: string;
  /** 重试按钮 testid */
  retryTestId?: string;
}

/** FormDrawer 滚动区（form-drawer__scroll-area）内的加载/错误/空态提示，
    铺满内容宽度居中；错误态为 danger 描边盒 + 重试按钮 */
export function DrawerStateMessage({
  variant,
  message,
  retryLabel,
  onRetry,
  testId,
  retryTestId,
}: DrawerStateMessageProps) {
  if (variant === 'error') {
    return (
      <div className="ui-drawer-state-error" role="alert" data-testid={testId}>
        <span>{message}</span>
        {retryLabel ? (
          <button type="button" onClick={onRetry} data-testid={retryTestId}>
            {retryLabel}
          </button>
        ) : null}
      </div>
    );
  }
  return (
    <div
      className="py-10 text-center text-[13px] text-text-muted"
      role={variant === 'loading' ? 'status' : undefined}
      data-testid={testId}
    >
      {message}
    </div>
  );
}
