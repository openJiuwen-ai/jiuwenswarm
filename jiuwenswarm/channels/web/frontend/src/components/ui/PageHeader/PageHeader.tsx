import type { ReactNode } from 'react';

export interface PageHeaderProps {
  title: ReactNode;
  subtitle?: ReactNode;
  children?: ReactNode;
  titleTestId?: string;
  subtitleTestId?: string;
}

export function PageHeader({ title, subtitle, children, titleTestId, subtitleTestId }: PageHeaderProps) {
  return (
    <div className="flex items-start justify-between" data-testid="common-page-header">
      <div>
        <h2 className="h-9 font-semibold text-[24px] leading-[36px]" data-testid={titleTestId}>{title}</h2>
        {subtitle && (
          <p className="text-sm text-text-muted mt-1" data-testid={subtitleTestId}>{subtitle}</p>
        )}
      </div>
      {children && <div className="flex items-center">{children}</div>}
    </div>
  );
}
