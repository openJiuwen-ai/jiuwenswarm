import { useLayoutEffect, useRef, useState } from 'react';
import { useAdaptiveTooltip } from '../../hooks/useAdaptiveTooltip';

interface TruncatedTextProps {
  text: string;
  className?: string;
}

export function TruncatedText({ text, className }: TruncatedTextProps) {
  const ref = useRef<HTMLParagraphElement>(null);
  const [truncated, setTruncated] = useState(false);
  const { tooltip, handlers } = useAdaptiveTooltip({ maxWidth: 320 });

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const check = () => setTruncated(el.scrollHeight > el.clientHeight + 1);
    check();
    const observer = new ResizeObserver(check);
    observer.observe(el);
    return () => observer.disconnect();
  }, [text]);

  return (
    <>
      <p
        ref={ref}
        className={className}
        data-tooltip={truncated ? text : undefined}
        {...(truncated ? handlers : {})}
      >
        {text}
      </p>
      {truncated && tooltip}
    </>
  );
}
