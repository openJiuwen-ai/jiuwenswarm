import { type ReactNode } from 'react';
import emptyStateUrl from './empty-state.svg';

export interface EmptyStateProps {
  id: string;
  text: string;
  className?: string;
  children?: ReactNode;
}

export function EmptyState({ id, text, className, children }: EmptyStateProps) {
  return (
    <div
      className={`flex w-full flex-col items-center justify-center gap-[8px] py-16${className ? ` ${className}` : ''}`}
      data-testid={id}
    >
      <img src={emptyStateUrl} alt="" width={80} height={80} />
      <div className="text-[12px] text-text-muted">{text}</div>
      {children}
    </div>
  );
}
