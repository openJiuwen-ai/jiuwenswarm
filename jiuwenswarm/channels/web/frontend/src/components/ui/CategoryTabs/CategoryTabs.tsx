import type { ReactNode } from 'react';
import { useLayoutEffect } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';

import { useHorizontalScrollEdges } from '../../../hooks';

import './CategoryTabs.css';

export interface CategoryTabsOption<T extends string = string> {
  value: T;
  label: ReactNode;
}

export interface CategoryTabsProps<T extends string = string> {
  items: CategoryTabsOption<T>[];
  value: T;
  onChange: (value: T) => void;
}

export function CategoryTabs<T extends string = string>({ items, value, onChange }: CategoryTabsProps<T>) {
  const { ref: scrollRef, canScrollLeft, canScrollRight, update: updateScrollState } = useHorizontalScrollEdges<HTMLDivElement>();

  useLayoutEffect(() => {
    updateScrollState();
  }, [items, updateScrollState]);

  const scrollByPage = (direction: 1 | -1) => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollBy({ left: direction * el.clientWidth * 0.8, behavior: 'smooth' });
  };

  return (
    <div
      className={`categoryTabs${canScrollLeft ? ' is-scroll-left' : ''}${canScrollRight ? ' is-scroll-right' : ''}`}
      data-testid="categoryTabs"
    >
      <button
        type="button"
        className="categoryTabs__scroll categoryTabs__scroll--prev"
        aria-label="Scroll left"
        onClick={() => scrollByPage(-1)}
        data-hidden={!canScrollLeft}
      >
        <ChevronLeft size={16} aria-hidden="true" />
      </button>
      <div className="categoryTabs__viewport" ref={scrollRef} onScroll={updateScrollState}>
        {items.map((item, idx) => (
          <span key={item.value} className="flex items-center">
            {idx > 0 && <span className="inline-flex items-center h-4 text-text-divider px-4">|</span>}
            <button
              type="button"
              onClick={() => onChange(item.value)}
              className={`whitespace-nowrap ${
                value === item.value
                  ? 'text-text font-bold'
                  : 'text-text-weak hover:text-text'
              }`}
            >
              {item.label}
            </button>
          </span>
        ))}
      </div>
      <button
        type="button"
        className="categoryTabs__scroll categoryTabs__scroll--next"
        aria-label="Scroll right"
        onClick={() => scrollByPage(1)}
        data-hidden={!canScrollRight}
      >
        <ChevronRight size={16} aria-hidden="true" />
      </button>
    </div>
  );
}
