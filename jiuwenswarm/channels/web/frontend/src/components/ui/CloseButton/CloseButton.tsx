/** 弹窗右上角关闭按钮，仅暴露 size（px，默认 24）与 onClick，样式内聚不透传。 */
export function CloseButton({
  size = 24,
  onClick,
  testId = 'ui-close-button',
}: {
  size?: number;
  onClick: () => void;
  testId?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label="Close"
      data-testid={testId}
      className="flex items-center justify-center rounded-md text-text-meta hover:text-text"
      style={{ width: size, height: size }}
    >
      <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
      </svg>
    </button>
  );
}
