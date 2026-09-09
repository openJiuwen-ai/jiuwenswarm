import { useEffect, useRef, useState, type InputHTMLAttributes } from 'react';

export interface PageToolbarSearchProps extends InputHTMLAttributes<HTMLInputElement> {
  wrapperTestId?: string;
  inputTestId?: string;
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

export function PageToolbarSearch({ wrapperTestId, inputTestId, className, ...rest }: PageToolbarSearchProps) {
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(200);

  useEffect(() => {
    const container = wrapperRef.current?.closest('.app-page-body');
    if (!container) return undefined;
    const observer = new ResizeObserver(entries => {
      const entry = entries[entries.length - 1];
      if (entry) setWidth(resolveWrapperWidth(entry.contentRect.width));
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  return (
    <div ref={wrapperRef} data-testid={wrapperTestId} className="relative flex-shrink-0" style={{ width }}>
      <svg className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-text-muted pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-4.35-4.35M11 19a8 8 0 100-16 8 8 0 000 16z" />
      </svg>
      <input
        data-testid={inputTestId}
        {...rest}
        className={`w-full pl-8 pr-3 py-1.5 rounded-[6px] border border-border text-sm text-text placeholder:text-text-muted focus-visible:outline-none focus-visible:shadow-none${className ? ` ${className}` : ''}`}
      />
    </div>
  );
}
