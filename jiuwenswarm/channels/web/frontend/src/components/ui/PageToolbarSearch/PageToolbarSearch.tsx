import { useEffect, useRef, useState, type InputHTMLAttributes } from 'react';

export interface PageToolbarSearchProps extends InputHTMLAttributes<HTMLInputElement> {
  wrapperTestId?: string;
  inputTestId?: string;
  clearTestId?: string;
  onClear?: () => void;
  /** 宽度测量容器选择器，默认 '.app-page-body'；页面不在 app-page-body 内时（如 CronPanel）传自己的页面根节点 */
  measureSelector?: string;
}

const WIDTH_STEPS: Array<[number, number]> = [
  [1528, 404],
  [1328, 355],
  [1208, 320],
  [888, 228],
];

function resolveWrapperWidth(containerWidth: number): number {
  for (const [minWidth, width] of WIDTH_STEPS) {
    if (containerWidth >= minWidth) return width;
  }
  return 200;
}

export function PageToolbarSearch({
  wrapperTestId,
  inputTestId,
  clearTestId,
  onClear,
  measureSelector,
  className,
  ...rest
}: PageToolbarSearchProps) {
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(200);

  useEffect(() => {
    const container = wrapperRef.current?.closest(measureSelector ?? '.app-page-body');
    if (!container) return undefined;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[entries.length - 1];
      if (entry) setWidth(resolveWrapperWidth(entry.contentRect.width));
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, [measureSelector]);

  const hasValue = String(rest.value ?? '').length > 0;
  const showClear = !!onClear && hasValue;
  const derivedClearTestId = clearTestId ?? (inputTestId ? `${inputTestId.replace(/-input$/, '')}-clear` : undefined);

  return (
    <div ref={wrapperRef} data-testid={wrapperTestId} className="relative flex-shrink-0" style={{ width }}>
      <svg className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-text-muted pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-4.35-4.35M11 19a8 8 0 100-16 8 8 0 000 16z" />
      </svg>
      <input
        data-testid={inputTestId}
        {...rest}
        className={`w-full pl-8 ${showClear ? 'pr-7' : 'pr-3'} py-1.5 rounded-[6px] border border-[color:var(--color-border-toolbar-input)] text-[12px] text-text placeholder:text-[color:var(--color-text-placeholder)] focus-visible:outline-none focus-visible:shadow-none${className ? ` ${className}` : ''}`}
      />
      {showClear && (
        <button
          type="button"
          onClick={onClear}
          data-testid={derivedClearTestId}
          aria-label="clear"
          className="absolute right-2 top-1/2 -translate-y-1/2 flex h-4 w-4 items-center justify-center text-text-muted hover:text-text"
        >
          <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      )}
    </div>
  );
}
