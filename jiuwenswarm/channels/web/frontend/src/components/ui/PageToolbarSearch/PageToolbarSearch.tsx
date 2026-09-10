import { useEffect, useRef, useState } from 'react';
import { Search } from 'lucide-react';
import { Input, type InputProps } from '../Input/Input';
import './PageToolbarSearch.css';

export type PageToolbarSearchProps = Omit<InputProps, 'size' | 'prefix'> & {
  wrapperTestId?: string;
  inputTestId?: string;
};

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
  className,
  rootClassName,
  ...inputProps
}: PageToolbarSearchProps) {
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(200);

  useEffect(() => {
    const container = wrapperRef.current?.closest('.app-page-body');
    if (!container) return undefined;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[entries.length - 1];
      if (entry) setWidth(resolveWrapperWidth(entry.contentRect.width));
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  return (
    <div ref={wrapperRef} data-testid={wrapperTestId} className="relative flex-shrink-0" style={{ width }}>
      <Input
        {...inputProps}
        size="small"
        data-testid={inputTestId}
        className={className}
        rootClassName={`page-toolbar-search__input${rootClassName ? ` ${rootClassName}` : ''}`}
        prefix={<Search size={16} strokeWidth={1.5} aria-hidden="true" />}
      />
    </div>
  );
}
