import type { ReactNode, Ref } from 'react';

interface MarketplaceSurfaceProps {
  children: ReactNode;
  variant: 'catalog' | 'detail';
  scrollRef?: Ref<HTMLDivElement>;
  testId?: string;
}

/** Shared page geometry from the 2026-08-08 expert and extension market design. */
export function MarketplaceSurface({ children, variant, scrollRef, testId }: MarketplaceSurfaceProps) {
  return (
    <div
      ref={scrollRef}
      className={`marketplace-surface marketplace-surface--${variant} relative h-full overflow-y-auto bg-card`}
      data-testid={testId}
    >
      <div className={`mx-auto w-full max-w-[1400px] px-8 pb-10 ${variant === 'catalog' ? 'pt-16' : 'pt-12'}`}>
        {children}
      </div>
    </div>
  );
}
