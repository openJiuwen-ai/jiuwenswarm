import { useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import './CollapsibleText.css';

export type CollapsibleTextProps = {
  children: ReactNode;
  maxLines?: number;
  expandLabel: string;
  collapseLabel: string;
};

export function CollapsibleText({ children, maxLines = 3, expandLabel, collapseLabel }: CollapsibleTextProps) {
  const contentRef = useRef<HTMLSpanElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(false);

  useLayoutEffect(() => {
    const content = contentRef.current;
    if (!content) return;

    const updateOverflow = () => {
      const lineHeight = Number.parseFloat(window.getComputedStyle(content).lineHeight);
      if (!Number.isFinite(lineHeight)) return;
      const nextOverflowing = content.scrollHeight > lineHeight * maxLines + 0.5;
      setOverflowing((current) => (current === nextOverflowing ? current : nextOverflowing));
      if (!nextOverflowing) setExpanded(false);
    };

    updateOverflow();
    const observer = new ResizeObserver(updateOverflow);
    observer.observe(content);
    return () => observer.disconnect();
  }, [children, maxLines]);

  return (
    <span className="collapsible-text">
      <span
        ref={contentRef}
        className={`collapsible-text__content${expanded ? ' collapsible-text__content--expanded' : ''}`}
        style={{ WebkitLineClamp: maxLines }}
      >
        {children}
      </span>
      {overflowing ? (
        <button
          type="button"
          className="collapsible-text__toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? collapseLabel : expandLabel}
        </button>
      ) : null}
    </span>
  );
}
