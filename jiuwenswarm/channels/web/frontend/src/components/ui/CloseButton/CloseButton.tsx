/** 弹窗/面板关闭按钮，暴露 size（px，默认 24）、可选无障碍文案与 onClick，样式内聚不透传。 */
export function CloseButton({
  size = 24,
  onClick,
  testId = 'ui-close-button',
  ariaLabel = 'Close',
  title,
}: {
  size?: number;
  onClick: () => void;
  testId?: string;
  ariaLabel?: string;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={ariaLabel}
      title={title}
      data-testid={testId}
      className="flex items-center justify-center rounded-md text-text-meta hover:text-text"
      style={{ width: size, height: size }}
    >
      <svg width={size} height={size} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
      </svg>
    </button>
  );
}
